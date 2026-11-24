"""关账 × 脉冲 争用协议测试（口径B：在途脉冲优先，完成后再关）。

覆盖题目点名夹具：
  1) 关账抢先：屏障之后登记的脉冲被整笔拒绝（无脉冲行、不推进累计）。
  2) 脉冲先进：屏障之前已登记的在途脉冲必须落账，关账须带上该增量。
  3) 半截修复：桩已释放/占用与状态机分裂时，修复回滚到争用结局一致快照。
  4) 回执成功却无对应电量行：重放核对能识别并由修复对齐。
另含：真实多连接并发压测、在途超时清扫、关账等待超时 409、已关不再增电量。

运行：PYTHONPATH=. python -m unittest discover -s tests -v
"""
from __future__ import annotations
import os
import tempfile
import threading
import unittest
from uuid import uuid4

# 在导入 app 之前指定独立临时库，避免污染开发库
_TMP = tempfile.mkdtemp(prefix="chargebout-test-")
_DB = os.path.join(_TMP, f"test-{uuid4().hex}.db")
os.environ["CHARGEBOUT_DB"] = _DB
os.environ["CHARGEBOUT_INFLIGHT_TIMEOUT"] = "30"
os.environ["CHARGEBOUT_SETTLE_DRAIN_TIMEOUT"] = "3"

from fastapi import HTTPException  # noqa: E402

from app.db import db, init_db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import contention, core, sessions as svc  # noqa: E402


class ContentionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        seed_if_empty()

    def setUp(self):
        # 每个用例独立：清空可变业务数据并复位桩枪与序号，避免桩位耗尽与互相污染
        with db() as conn:
            for t in ("contention_journal", "inflight_pulses", "pulses",
                      "session_events", "sessions"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("UPDATE piles SET status='空闲' WHERE status IN ('占用','故障')")
            conn.execute("UPDATE guns SET status='可用' WHERE status='占用'")
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('会话序号','1000') "
                "ON CONFLICT(key) DO UPDATE SET value='1000'"
            )

    def open_one(self) -> int:
        with db() as conn:
            pile = conn.execute(
                "SELECT id FROM piles WHERE status='空闲' ORDER BY id LIMIT 1"
            ).fetchone()
            r = svc.open_session(conn, pile["id"])
            return r["会话编号"]

    def get(self, session_id: int):
        with db() as conn:
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            pile = conn.execute(
                "SELECT p.status FROM piles p JOIN sessions s ON s.pile_id=p.id WHERE s.id=?",
                (session_id,),
            ).fetchone()
            gun = conn.execute(
                "SELECT g.status FROM guns g JOIN sessions s ON s.gun_id=g.id WHERE s.id=?",
                (session_id,),
            ).fetchone()
            return dict(se), pile["status"], gun["status"]

    def pulse_count(self, session_id: int) -> int:
        with db() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM pulses WHERE session_id=?", (session_id,)
            ).fetchone()["n"]


class TestBarrierRejectsLatePulse(ContentionTestBase):
    """夹具1：关账抢先——屏障之后登记的脉冲整笔失败。"""

    def test_pulse_after_barrier_rejected_no_row_no_advance(self):
        sid = self.open_one()
        svc.add_pulse(sid, 1.0)  # 先正常一笔
        before = self.get(sid)[0]

        # 立关账屏障（不提交最终关账），世代+1、状态=结算中
        barrier = contention.begin_settle_barrier(sid)
        self.assertEqual(barrier["在途未收口"], 0)

        # 屏障之后再发脉冲：登记段直接 409（结算中且登记世代落后）
        with self.assertRaises(HTTPException) as cm:
            svc.add_pulse(sid, 2.0)
        self.assertEqual(cm.exception.status_code, 409)

        after = self.get(sid)[0]
        self.assertEqual(self.pulse_count(sid), 1, "被拒脉冲不得留 pulses 行")
        self.assertEqual(after["kwh"], before["kwh"], "被拒脉冲不得推进累计电量")
        self.assertEqual(after["amount"], before["amount"], "被拒脉冲不得推进金额")

        # 收口为零，关账可提交且电量不含被拒的 2.0
        receipt = contention.commit_settle(sid)
        self.assertEqual(receipt["结果"], "已关账")
        self.assertAlmostEqual(receipt["电量"], 1.0, places=3)

        closed = self.get(sid)[0]
        self.assertEqual(closed["status"], "已关账")
        # 已关账后再来脉冲，依然整笔拒绝，绝不长期已关仍增电量
        with self.assertRaises(HTTPException):
            svc.add_pulse(sid, 3.0)
        self.assertEqual(self.get(sid)[0]["kwh"], 1.0)


class TestInflightPulseLandsBeforeClose(ContentionTestBase):
    """夹具2：脉冲先进——屏障前已登记在途，允许落账，关账须带上增量。"""

    def test_registered_inflight_lands_and_settle_includes_it(self):
        sid = self.open_one()
        # 手动模拟“脉冲已登记在途、尚未落账”的并发瞬间
        token = contention.register_inflight(sid, 5.0, 7.0)
        with db() as conn:
            open_n = contention._open_inflight_count(conn, sid)
        self.assertEqual(open_n, 1)

        # 此时立屏障：屏障不拒绝早于它登记的在途
        barrier = contention.begin_settle_barrier(sid)
        self.assertEqual(barrier["在途未收口"], 1, "总览/关账不得假装零在途")

        # 在途脉冲完成落账（世代早于屏障，允许）
        fin = contention._finalize_inflight(token, sid, 5.0, 7.0)
        self.assertEqual(fin["结果"], "入账")
        self.assertIsNotNone(fin["脉冲行"])

        # 关账入口：屏障已在，等待收口后提交，金额须含 5.0
        receipt = contention.settle(sid)
        self.assertEqual(receipt["结果"], "已关账")
        self.assertAlmostEqual(receipt["电量"], 5.0, places=3)
        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(pile_status, "空闲")
        self.assertEqual(gun_status, "可用")
        # 金额 = 5.0 × 锁定电价
        self.assertAlmostEqual(receipt["金额"], round(5.0 * se["price_locked"], 2), places=2)

    def test_settle_blocks_while_inflight_open_then_succeeds(self):
        sid = self.open_one()
        token = contention.register_inflight(sid, 2.5, 7.0)

        def finish():
            import time
            time.sleep(0.3)
            contention._finalize_inflight(token, sid, 2.5, 7.0)

        t = threading.Thread(target=finish)
        t.start()
        # 关账与在途收口并发：settle 必须等到在途落账后才提交，并带上增量
        receipt = contention.settle(sid)
        t.join()
        self.assertAlmostEqual(receipt["电量"], 2.5, places=3)
        self.assertEqual(receipt["在途收口"], True)


class TestHalfStateRepair(ContentionTestBase):
    """夹具3：半截——占用已清却仍充电中（或其逆），修复回滚到一致快照。"""

    def test_released_but_still_charging_gets_repaired(self):
        sid = self.open_one()
        svc.add_pulse(sid, 3.0)
        # 人为制造半截：会话仍“充电中”，但桩枪已被释放
        with db() as conn:
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
            conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))

        with db() as conn:
            rep = contention.replay_session(conn, sid)
        self.assertFalse(rep["一致"])
        self.assertIn("桩位状态", rep)

        fixed = contention.repair_session(sid)
        self.assertTrue(fixed["修复后"]["一致"])
        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(se["status"], "充电中")
        self.assertEqual(pile_status, "占用", "在充会话修复后桩位须回到占用")
        self.assertEqual(gun_status, "占用")

    def test_closed_but_pile_still_occupied_gets_repaired(self):
        sid = self.open_one()
        svc.add_pulse(sid, 2.0)
        contention.settle(sid)
        # 制造半截：已关账但桩位仍占用
        with db() as conn:
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            conn.execute("UPDATE piles SET status='占用' WHERE id=?", (se["pile_id"],))
        fixed = contention.repair_session(sid)
        self.assertTrue(fixed["修复后"]["一致"])
        _, pile_status, _ = self.get(sid)
        self.assertEqual(pile_status, "空闲")


class TestReceiptWithoutPulseRow(ContentionTestBase):
    """夹具4：回执成功（登记标完成）却无对应电量行——重放识别并修复。"""

    def test_done_inflight_missing_pulse_row_detected_and_fixed(self):
        sid = self.open_one()
        token = contention.register_inflight(sid, 4.0, 7.0)
        contention._finalize_inflight(token, sid, 4.0, 7.0)
        # 人为删除对应电量行，但在途登记仍标“完成”
        with db() as conn:
            reg = conn.execute(
                "SELECT * FROM inflight_pulses WHERE token=?", (token,)
            ).fetchone()
            conn.execute("DELETE FROM pulses WHERE id=?", (reg["pulse_row_id"],))
            # 同时把会话累计改回 0，制造账面与登记分裂
            conn.execute("UPDATE sessions SET kwh=0, amount=0 WHERE id=?", (sid,))

        with db() as conn:
            rep = contention.replay_session(conn, sid)
        self.assertFalse(rep["一致"])
        self.assertTrue(any("无对应电量行" in d for d in rep["偏差"]))

        fixed = contention.repair_session(sid)
        # 修复以现存 pulses 行为真值：电量行已被删，重算电量归零并对齐金额/计数
        self.assertTrue(fixed["修复后"]["一致"])
        se, _, _ = self.get(sid)
        self.assertEqual(se["kwh"], 0.0)


class TestInflightSweepAndDrainTimeout(ContentionTestBase):
    def test_stale_inflight_swept_then_settle_succeeds(self):
        sid = self.open_one()
        # 登记后不落账，模拟脉冲进程在落账前崩溃：显式强制超时清扫该残留
        contention.register_inflight(sid, 1.0, 0)
        with db() as conn:
            from app.db import immediate_tx
            with immediate_tx(conn):
                n = contention.sweep_stale_inflight(conn, sid, force=True)
        self.assertEqual(n, 1)
        receipt = contention.settle(sid)  # 残留已收口，关账可提交
        self.assertEqual(receipt["结果"], "已关账")
        with db() as conn:
            self.assertEqual(contention._open_inflight_count(conn, sid), 0)
            # 被清扫的在途未入账：无 pulses 行、电量为 0
            kwh = conn.execute(
                "SELECT COALESCE(SUM(delta_kwh),0) k FROM pulses WHERE session_id=?",
                (sid,),
            ).fetchone()["k"]
        self.assertEqual(kwh, 0.0)

    def test_drain_timeout_returns_409_and_keeps_settling(self):
        sid = self.open_one()
        # 造一笔不会被清扫（很新）也不完成的在途，撑过 drain 窗口
        contention.register_inflight(sid, 1.0, 0)
        with self.assertRaises(HTTPException) as cm:
            contention.settle(sid)
        self.assertEqual(cm.exception.status_code, 409)
        se, _, _ = self.get(sid)
        self.assertEqual(se["status"], "结算中", "超时后停留结算中，无半截、可重试")
        # 总览不得假装零在途
        with db() as conn:
            ov = core.overview(conn)
        self.assertGreaterEqual(ov["在途脉冲数"], 1)


class TestInFlightBlocksTerminalTransitions(ContentionTestBase):
    """在途未收口时，超时/中断等终态转换必须让路（口径不混用）。"""

    def test_timeout_and_abort_rejected_while_inflight_open(self):
        sid = self.open_one()
        contention.register_inflight(sid, 1.0, 0)  # 在途不收口
        with db() as conn:
            with self.assertRaises(HTTPException) as cm:
                svc.mark_timeout(conn, sid)
            self.assertEqual(cm.exception.status_code, 409)
        with db() as conn:
            with self.assertRaises(HTTPException) as cm:
                svc.abort_session(conn, sid)
            self.assertEqual(cm.exception.status_code, 409)
        # 收口后即可正常中断，且桩枪释放
        with db() as conn:
            from app.db import immediate_tx
            with immediate_tx(conn):
                contention.sweep_stale_inflight(conn, sid, force=True)
        with db() as conn:
            r = svc.abort_session(conn, sid, "急停")
        self.assertEqual(r["状态"], "已中断")
        _, pile_status, gun_status = self.get(sid)
        self.assertEqual(pile_status, "空闲")
        self.assertEqual(gun_status, "可用")


class TestConcurrentStress(ContentionTestBase):
    """真实多连接并发：多脉冲与关账同时进行，结局必须线性一致。"""

    def test_many_pulses_vs_settle(self):
        sid = self.open_one()
        errors: list[Exception] = []

        def pulses():
            for _ in range(10):
                try:
                    svc.add_pulse(sid, 0.5)
                except HTTPException as e:
                    errors.append(e)

        t = threading.Thread(target=pulses)
        t.start()
        # 给脉冲一点启动时间，再并发关账
        import time
        time.sleep(0.05)
        settle_err = None
        try:
            contention.settle(sid)
        except HTTPException as e:
            settle_err = e
        t.join()

        se, pile_status, gun_status = self.get(sid)
        with db() as conn:
            row_kwh = conn.execute(
                "SELECT COALESCE(SUM(delta_kwh),0) k FROM pulses WHERE session_id=?",
                (sid,),
            ).fetchone()["k"]
            open_n = contention._open_inflight_count(conn, sid)
            rep = contention.replay_session(conn, sid)

        # 不变量：账面电量恒等于脉冲行重算；无未收口在途；状态与占用一致
        self.assertAlmostEqual(se["kwh"], round(row_kwh, 3), places=3)
        self.assertEqual(open_n, 0)
        self.assertTrue(rep["一致"], rep["偏差"])
        if settle_err is None:
            self.assertEqual(se["status"], "已关账")
            self.assertEqual(pile_status, "空闲")
            self.assertEqual(gun_status, "可用")
            # 已关账后 kwh 冻结
            with self.assertRaises(HTTPException):
                svc.add_pulse(sid, 1.0)
            self.assertEqual(self.get(sid)[0]["kwh"], se["kwh"])


class TestSettleReceiptTruth(ContentionTestBase):
    """夹具：关账绿仍进脉冲——回执、状态机、腾空、总览、审计必须同刷同命运。"""

    def test_green_receipt_means_closed_everywhere(self):
        sid = self.open_one()
        svc.add_pulse(sid, 2.0)
        receipt = svc.settle(sid)  # 走 HTTP 入口同款 svc.settle
        self.assertEqual(receipt["结果"], "已关账")

        # 回执绿 ⇒ 会话已关态、桩枪已腾空（同命运）
        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(se["status"], "已关账")
        self.assertEqual(pile_status, "空闲")
        self.assertEqual(gun_status, "可用")

        # 关后脉冲一律拒写，电量金额冻结
        with self.assertRaises(HTTPException) as cm:
            svc.add_pulse(sid, 1.0)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.get(sid)[0]["kwh"], 2.0)
        self.assertEqual(self.pulse_count(sid), 1)

        # 总览如实：无涂改层、无旁路假绿键
        with db() as conn:
            ov = core.overview(conn)
            self.assertEqual(ov["已关账"], 1)
            self.assertEqual(ov["充电中"], 0)
            self.assertEqual(ov["桩位占用"], 0)
            self.assertNotIn("surface_ok", ov)
            keys = {r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()}
        self.assertFalse(any(k.startswith("surface") for k in keys), keys)
        self.assertFalse(any(k.startswith("settle_surface") for k in keys), keys)

        # 审计可重放：事件流只有一套终态，重放一致
        with db() as conn:
            rep = contention.replay_session(conn, sid)
            actions = [r["action"] for r in conn.execute(
                "SELECT action FROM session_events WHERE session_id=? ORDER BY id", (sid,)
            ).fetchall()]
        self.assertTrue(rep["一致"], rep["偏差"])
        self.assertEqual(actions.count("结算关账"), 1)
        self.assertNotIn("异常中断", actions)


class TestAbortSettleRace(ContentionTestBase):
    """夹具：急停关账交错双终态——两个时序都必须收敛到单一终态。"""

    def test_abort_wins_then_late_commit_abandoned(self):
        sid = self.open_one()
        svc.add_pulse(sid, 2.0)
        contention.begin_settle_barrier(sid)  # 关账屏障已立（结算中）
        with db() as conn:
            r = svc.abort_session(conn, sid, "急停")  # 急停抢先收口
        self.assertEqual(r["状态"], "已中断")

        # 迟到的关账临界提交必须整笔放弃，不得把 已中断 改写成 已关账
        with self.assertRaises(HTTPException) as cm:
            contention.commit_settle(sid)
        self.assertEqual(cm.exception.status_code, 409)

        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(se["status"], "已中断", "急停结局不得被关账临界覆盖")
        self.assertEqual(pile_status, "空闲")
        self.assertEqual(gun_status, "可用")
        with db() as conn:
            actions = [r["action"] for r in conn.execute(
                "SELECT action FROM session_events WHERE session_id=? ORDER BY id", (sid,)
            ).fetchall()]
            rep = contention.replay_session(conn, sid)
        self.assertEqual(actions.count("异常中断"), 1)
        self.assertNotIn("结算关账", actions, "禁止双终态事件同挂")
        self.assertTrue(rep["一致"], rep["偏差"])

    def test_settle_wins_then_abort_rejected(self):
        sid = self.open_one()
        svc.add_pulse(sid, 1.0)
        receipt = svc.settle(sid)
        self.assertEqual(receipt["结果"], "已关账")
        with db() as conn:
            with self.assertRaises(HTTPException):
                svc.abort_session(conn, sid, "急停")
        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(se["status"], "已关账")
        self.assertEqual(pile_status, "空闲")
        self.assertEqual(gun_status, "可用")


class TestTimeoutSettleRace(ContentionTestBase):
    """夹具：超时关账双终态——超时与关账交错只允许一套终态字。"""

    def test_timeout_then_settle_single_final(self):
        sid = self.open_one()
        svc.add_pulse(sid, 1.5)
        with db() as conn:
            r = svc.mark_timeout(conn, sid)
        self.assertEqual(r["状态"], "已超时")
        # 已超时会话仍可关账收口：终态字只有 已关账 一套
        receipt = svc.settle(sid)
        self.assertEqual(receipt["结果"], "已关账")
        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(se["status"], "已关账")
        self.assertEqual(pile_status, "空闲")
        self.assertEqual(gun_status, "可用")
        with db() as conn:
            rep = contention.replay_session(conn, sid)
        self.assertTrue(rep["一致"], rep["偏差"])

    def test_barrier_then_timeout_rejected(self):
        sid = self.open_one()
        contention.begin_settle_barrier(sid)  # 结算中
        with db() as conn:
            with self.assertRaises(HTTPException) as cm:
                svc.mark_timeout(conn, sid)
        self.assertEqual(cm.exception.status_code, 400)
        receipt = svc.settle(sid)  # 关账不受干扰，单套终态
        self.assertEqual(receipt["结果"], "已关账")
        self.assertEqual(self.get(sid)[0]["status"], "已关账")


class TestOpenThenImmediateContention(ContentionTestBase):
    """夹具：开充入口新建会话后立即关账×脉冲对撞，结局仍须线性一致。"""

    def test_fresh_session_pulse_vs_settle(self):
        sid = self.open_one()  # 新建会话立即进入对撞
        token = contention.register_inflight(sid, 3.0, 7.0)
        outcomes: dict = {}

        def settle():
            try:
                outcomes["settle"] = contention.settle(sid)
            except HTTPException as e:
                outcomes["settle_err"] = e

        t = threading.Thread(target=settle)
        t.start()
        fin = contention._finalize_inflight(token, sid, 3.0, 7.0)
        t.join()
        self.assertEqual(fin["结果"], "入账", "屏障前登记的在途必须落账")
        self.assertIn("settle", outcomes)
        self.assertAlmostEqual(outcomes["settle"]["电量"], 3.0, places=3)
        se, pile_status, gun_status = self.get(sid)
        self.assertEqual(se["status"], "已关账")
        self.assertEqual((pile_status, gun_status), ("空闲", "可用"))
        with db() as conn:
            self.assertTrue(contention.replay_session(conn, sid)["一致"])


class TestTerminalOccupancyReconcile(ContentionTestBase):
    """占用钉 ↔ 腾空谓词全状态对账：已中断/已超时 的半截也能被重放识别并修复。"""

    def test_aborted_but_pile_occupied_gets_repaired(self):
        sid = self.open_one()
        with db() as conn:
            svc.abort_session(conn, sid, "急停")
        with db() as conn:  # 制造半截：已中断但桩位仍占用
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            conn.execute("UPDATE piles SET status='占用' WHERE id=?", (se["pile_id"],))
            conn.execute("UPDATE guns SET status='占用' WHERE id=?", (se["gun_id"],))
        with db() as conn:
            rep = contention.replay_session(conn, sid)
        self.assertFalse(rep["一致"])
        fixed = contention.repair_session(sid)
        self.assertTrue(fixed["修复后"]["一致"])
        _, pile_status, gun_status = self.get(sid)
        self.assertEqual((pile_status, gun_status), ("空闲", "可用"))

    def test_timeout_but_pile_released_gets_repaired(self):
        sid = self.open_one()
        with db() as conn:
            svc.mark_timeout(conn, sid)
        with db() as conn:  # 制造半截：已超时（未结）但桩枪被腾空
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
            conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
        with db() as conn:
            rep = contention.replay_session(conn, sid)
        self.assertFalse(rep["一致"], "已超时未结会话必须钉住占用")
        fixed = contention.repair_session(sid)
        self.assertTrue(fixed["修复后"]["一致"])
        _, pile_status, gun_status = self.get(sid)
        self.assertEqual((pile_status, gun_status), ("占用", "占用"))


class TestBypassResidueConverged(ContentionTestBase):
    """假关账收口：既有库里的旁路涂改键在启动迁移时被清除。"""

    def test_migrate_purges_surface_keys(self):
        with db() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('surface_last','{}') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('surface_ok','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('settle_surface:1','{}') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            from app.db import migrate
            migrate(conn)
            keys = {r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()}
        self.assertNotIn("surface_last", keys)
        self.assertNotIn("surface_ok", keys)
        self.assertNotIn("settle_surface:1", keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
