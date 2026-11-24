"""关账 × 脉冲 争用协议（全局唯一口径 B：在途脉冲优先，完成后再关）。

口径约定（任何入口都不得与此分叉）：

* 脉冲两段式：
  1) 登记段：在 ``inflight_pulses`` 插入一条“在途”登记（独立短事务，立即可见），
     同时把会话 ``inflight_count+1``。
  2) 落账段：``BEGIN IMMEDIATE`` 取写锁后复查会话状态/世代：
       - 仍可充（充电中，或结算中但本登记早于关账屏障世代）→ 落账，登记置“完成”。
       - 已关账 / 已超时 / 已中断，或登记晚于关账屏障 → 整笔拒绝：不写 pulses 行、
         不推进 kwh/amount，登记置“超时”并回收 inflight_count，回执明确被拒。
* 关账两段式：
  1) 屏障段：``BEGIN IMMEDIATE`` 把“充电中”会话推进“结算中”并令 settle_generation+1。
     屏障之后新登记的脉冲一律拒绝；屏障之前已登记的在途脉冲允许落账。
  2) 收口段：轮询等待该会话所有“在途”登记收口（完成或超时清扫），全部清零后才进入
     临界提交；等待超时则整笔失败，会话停留“结算中”，可安全重试（无半截、可重放）。
  3) 临界段：在同一写事务内写争用前快照、落定金额、状态置“已关账”、清空桩枪占用。

重放：``replay_session`` 以 pulses 行重算累计电量/金额，并核对在途登记、会话状态机与
桩枪占用的一致性；``repair_session`` 按重算真值把任何半截状态修复到争用结局。
"""
from __future__ import annotations
import json
import time
import uuid
from datetime import datetime

from fastapi import HTTPException

from app.config import (
    INFLIGHT_TIMEOUT_SECONDS,
    SETTLE_DRAIN_POLL_SECONDS,
    SETTLE_DRAIN_TIMEOUT_SECONDS,
)
from app.db import connect, immediate_tx
from app.services.core import current_band_price, now_str

# 在途登记生命周期终态
INFLIGHT_OPEN = "在途"
INFLIGHT_DONE = "完成"
INFLIGHT_TIMEOUT = "超时"


# ───────────────────────────── 时间/工具 ─────────────────────────────

def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def _age_seconds(ts: str) -> float:
    return (datetime.now() - _parse(ts)).total_seconds()


def _new_token() -> str:
    return uuid.uuid4().hex


def _price_of(conn, session) -> float:
    return float(session["price_locked"] or current_band_price(conn, session["rate_plan_id"]))


# ───────────────────────── 崩溃残留清扫（在途超时）─────────────────────────

def sweep_stale_inflight(conn, session_id: int | None = None, force: bool = False) -> int:
    """把超过存活阈值仍“在途”的登记标记为超时并回收计数。须在写事务内调用。

    返回被清扫的登记数。这些登记对应脉冲进程在落账前崩溃，按口径 B 不允许其再落账，
    但必须有着落（超时终态 + 事件流水），不留无主在途。
    ``force=True`` 时忽略时间阈值（用于崩溃恢复/测试显式收口）。
    """
    rows = conn.execute(
        "SELECT * FROM inflight_pulses WHERE status=?", (INFLIGHT_OPEN,)
    ).fetchall()
    swept = 0
    ts = now_str()
    for r in rows:
        if session_id is not None and r["session_id"] != session_id:
            continue
        if not force and _age_seconds(r["registered_at"]) < INFLIGHT_TIMEOUT_SECONDS:
            continue
        conn.execute(
            "UPDATE inflight_pulses SET status=?, completed_at=?, note=? WHERE id=?",
            (INFLIGHT_TIMEOUT, ts, "在途超时清扫（崩溃残留，未入账）", r["id"]),
        )
        conn.execute(
            "UPDATE sessions SET inflight_count=MAX(0, inflight_count-1) WHERE id=?",
            (r["session_id"],),
        )
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (r["session_id"], "在途超时", ts, f"登记{r['token'][:8]} 未入账即超时"),
        )
        swept += 1
    return swept


def _open_inflight_count(conn, session_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM inflight_pulses WHERE session_id=? AND status=?",
        (session_id, INFLIGHT_OPEN),
    ).fetchone()["n"]


# ─────────────────────────────── 脉冲两段式 ───────────────────────────────

def register_inflight(session_id: int, delta_kwh: float, power_kw: float) -> str:
    """登记段：插入可见的在途登记并 inflight_count+1。返回 token。"""
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    conn = connect(writer=True)
    try:
        with immediate_tx(conn):
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not se:
                raise HTTPException(404, detail="会话不存在")
            # 仅“充电中”可登记在途；结算中（关账屏障已立）及一切终态一律拒绝新登记
            if se["status"] != "充电中":
                raise HTTPException(
                    409,
                    detail={
                        "结果": "拒绝",
                        "原因": f"会话状态为{se['status']}，关账屏障已立或已收口，新在途登记被拒",
                        "会话编号": session_id,
                        "脉冲行": None,
                        "累计电量": round(float(se["kwh"]), 3),
                    },
                )
            token = _new_token()
            ts = now_str()
            conn.execute(
                """INSERT INTO inflight_pulses
                   (token,session_id,delta_kwh,power_kw,status,registered_at,settle_generation)
                   VALUES(?,?,?,?,?,?,?)""",
                (token, session_id, delta_kwh, power_kw, INFLIGHT_OPEN, ts,
                 se["settle_generation"]),
            )
            conn.execute(
                "UPDATE sessions SET inflight_count=inflight_count+1 WHERE id=?",
                (session_id,),
            )
        return token
    finally:
        conn.close()


def _finalize_inflight(token: str, session_id: int, delta_kwh: float,
                       power_kw: float) -> dict:
    """落账段：取写锁复查，决定落账或整笔拒绝。"""
    conn = connect(writer=True)
    try:
        with immediate_tx(conn):
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            reg = conn.execute(
                "SELECT * FROM inflight_pulses WHERE token=?", (token,)
            ).fetchone()
            if not reg:
                raise HTTPException(404, detail="在途登记不存在")
            if reg["status"] != INFLIGHT_OPEN:
                # 已被超时清扫，禁止再入账
                raise HTTPException(409, detail="在途脉冲已超时收口，不可入账")
            ts = now_str()
            # 关账屏障判定（口径B）：
            #   充电中 → 放行；
            #   结算中 → 仅“世代更早（屏障之前已登记）”的在途允许落账，
            #            同代/更新意味着屏障之后登记，整笔拒绝；
            #   已关账/已超时/已中断 → 整笔拒绝。
            if se["status"] in ("已关账", "已超时", "已中断"):
                barred = True
            elif se["status"] == "结算中":
                barred = reg["settle_generation"] >= se["settle_generation"]
            else:
                barred = False
            if barred:
                conn.execute(
                    "UPDATE inflight_pulses SET status=?, completed_at=?, note=? WHERE id=?",
                    (INFLIGHT_TIMEOUT, ts, f"关账屏障/会话{se['status']}，整笔拒绝未入账",
                     reg["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET inflight_count=MAX(0,inflight_count-1) WHERE id=?",
                    (session_id,),
                )
                conn.execute(
                    "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                    (session_id, "脉冲拒绝", ts,
                     f"+{delta_kwh} 被关账屏障拒绝，未推进累计"),
                )
                raise HTTPException(
                    409,
                    detail={
                        "结果": "拒绝",
                        "原因": f"会话{se['status']}，关账已立屏障，在途脉冲整笔失败",
                        "会话编号": session_id,
                        "在途令牌": token,
                        "脉冲行": None,
                        "累计电量": round(float(se["kwh"]), 3),
                    },
                )
            # 通过：落账（写 pulses 行 + 推进累计/金额，同一事务）
            cur = conn.execute(
                "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
                (session_id, delta_kwh, power_kw, ts),
            )
            new_kwh = round(float(se["kwh"]) + float(delta_kwh), 3)
            price = _price_of(conn, se)
            amount = round(new_kwh * price, 2)
            conn.execute(
                "UPDATE sessions SET kwh=?, amount=?, inflight_count=MAX(0,inflight_count-1) WHERE id=?",
                (new_kwh, amount, session_id),
            )
            conn.execute(
                "UPDATE inflight_pulses SET status=?, completed_at=?, pulse_row_id=? WHERE id=?",
                (INFLIGHT_DONE, ts, cur.lastrowid, reg["id"]),
            )
            conn.execute(
                "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                (session_id, "脉冲", ts, f"+{delta_kwh}（在途{token[:8]}）"),
            )
            return {
                "结果": "入账",
                "会话编号": session_id,
                "在途令牌": token,
                "脉冲行": cur.lastrowid,
                "累计电量": new_kwh,
                "预估金额": amount,
                "关账世代": se["settle_generation"],
            }
    finally:
        conn.close()


def record_pulse(session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    """脉冲入口：登记段 + 落账段。任一段失败都不留无主在途、不推进累计。"""
    token = register_inflight(session_id, delta_kwh, power_kw)
    try:
        return _finalize_inflight(token, session_id, delta_kwh, power_kw)
    except HTTPException:
        raise
    except Exception as exc:  # 落账段崩溃：登记交由超时清扫收口，绝不假装成功
        raise HTTPException(500, detail=f"脉冲落账失败，在途登记{token[:8]}待超时收口: {exc}")


# ─────────────────────────────── 关账两段式 ───────────────────────────────

def _snapshot(conn, session) -> dict:
    return {
        "会话编号": session["id"],
        "状态": session["status"],
        "电量": float(session["kwh"]),
        "金额": float(session["amount"]),
        "世代": session["settle_generation"],
        "在途数": session["inflight_count"],
        "桩位编号": session["pile_id"],
        "枪线编号": session["gun_id"],
        "时间": now_str(),
    }


def begin_settle_barrier(session_id: int) -> dict:
    """屏障段：充电中 → 结算中，世代+1。已是结算中则幂等返回当前屏障。"""
    conn = connect(writer=True)
    try:
        with immediate_tx(conn):
            sweep_stale_inflight(conn, session_id)
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not se:
                raise HTTPException(404, detail="会话不存在")
            ts = now_str()
            if se["status"] == "已关账":
                raise HTTPException(400, detail="会话已关账，不可重复关账")
            if se["status"] in ("已中断",):
                raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
            if se["status"] == "充电中":
                gen = se["settle_generation"] + 1
                conn.execute(
                    "UPDATE sessions SET status='结算中', settle_generation=? WHERE id=?",
                    (gen, session_id),
                )
                snap = _snapshot(conn, conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
                conn.execute(
                    "INSERT INTO contention_journal(session_id,generation,phase,snapshot,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (session_id, gen, "关账屏障", json.dumps(snap, ensure_ascii=False), ts),
                )
                conn.execute(
                    "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                    (session_id, "关账屏障", ts, f"世代{gen}，在途{_open_inflight_count(conn, session_id)}笔"),
                )
                current_gen = gen
            else:  # 结算中 / 已超时：复用既有屏障，支持安全重试
                current_gen = se["settle_generation"]
            open_n = _open_inflight_count(conn, session_id)
            return {"会话编号": session_id, "屏障世代": current_gen, "在途未收口": open_n}
    finally:
        conn.close()


def _drain(session_id: int, gen: int) -> int:
    """等待在途登记全部收口；超时抛 409（会话停留结算中，可重试）。"""
    deadline = time.monotonic() + SETTLE_DRAIN_TIMEOUT_SECONDS
    conn = connect(writer=True)
    try:
        while True:
            with immediate_tx(conn):
                sweep_stale_inflight(conn, session_id)
                open_n = _open_inflight_count(conn, session_id)
            if open_n == 0:
                return 0
            if time.monotonic() >= deadline:
                raise HTTPException(
                    409,
                    detail={
                        "结果": "关账等待超时",
                        "会话编号": session_id,
                        "屏障世代": gen,
                        "在途未收口": open_n,
                        "提示": "仍有在途脉冲未收口，关账未提交，会话停留结算中，可重试",
                    },
                )
            time.sleep(SETTLE_DRAIN_POLL_SECONDS)
    finally:
        conn.close()


def commit_settle(session_id: int) -> dict:
    """临界段：在途清零后，单事务同刷 状态/金额/桩枪/快照。"""
    conn = connect(writer=True)
    try:
        with immediate_tx(conn):
            sweep_stale_inflight(conn, session_id)
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not se:
                raise HTTPException(404, detail="会话不存在")
            if se["status"] == "已关账":
                raise HTTPException(400, detail="会话已关账，不可重复关账")
            if se["status"] not in ("结算中", "已超时"):
                # 终态对撞收口：屏障之后若急停等入口已改写状态机（如 已中断），
                # 临界提交必须整笔放弃，绝不把终态字二次改写（禁双终态）；
                # “充电中”同样拒绝——无屏障的临界提交本身就是协议违例。
                raise HTTPException(
                    409,
                    detail={
                        "结果": "关账临界放弃",
                        "会话编号": session_id,
                        "当前状态": se["status"],
                        "提示": "关账屏障之后会话状态已被其他入口改写，临界提交整笔放弃，不留双终态",
                    },
                )
            if _open_inflight_count(conn, session_id) != 0:
                # 双保险：临界区里仍有在途则绝不关账（不假装零在途）
                raise HTTPException(409, detail="仍有在途脉冲，禁止关账提交")
            ts = now_str()
            price = _price_of(conn, se)
            amount = round(float(se["kwh"]) * price, 2)
            snap = _snapshot(conn, se)
            conn.execute(
                "INSERT INTO contention_journal(session_id,generation,phase,snapshot,created_at)"
                " VALUES(?,?,?,?,?)",
                (session_id, se["settle_generation"], "关账临界",
                 json.dumps(snap, ensure_ascii=False), ts),
            )
            conn.execute(
                "UPDATE sessions SET status='已关账', amount=?, closed_at=? WHERE id=?",
                (amount, ts, session_id),
            )
            # 桩枪占用清空与会话关账同事务同刷
            conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
            conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
            conn.execute(
                "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                (session_id, "结算关账", ts,
                 f"世代{se['settle_generation']} 金额{amount} 在途已清零"),
            )
            return {
                "结果": "已关账",
                "会话编号": session_id,
                "关账世代": se["settle_generation"],
                "电量": float(se["kwh"]),
                "金额": amount,
                "状态": "已关账",
                "在途收口": True,
            }
    finally:
        conn.close()


def settle(session_id: int) -> dict:
    """关账入口：屏障 → 等在途收口 → 临界提交，全程可重放、无半截。"""
    barrier = begin_settle_barrier(session_id)
    _drain(session_id, barrier["屏障世代"])
    return commit_settle(session_id)


# ───────────────────────── 重放 / 修复（争用结局可重放）─────────────────────────

def replay_session(conn, session_id: int) -> dict | None:
    """以 pulses 行为真值重放，核对会话累计/金额/在途/状态机/桩枪占用的一致性。"""
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        return None
    pulse_rows = conn.execute(
        "SELECT COALESCE(SUM(delta_kwh),0) AS k, COUNT(*) AS n FROM pulses WHERE session_id=?",
        (session_id,),
    ).fetchone()
    replayed_kwh = round(float(pulse_rows["k"]), 3)
    price = _price_of(conn, se)
    replayed_amount = round(replayed_kwh * price, 2)
    open_n = _open_inflight_count(conn, session_id)
    pile = conn.execute("SELECT status FROM piles WHERE id=?", (se["pile_id"],)).fetchone()
    gun = conn.execute("SELECT status FROM guns WHERE id=?", (se["gun_id"],)).fetchone()

    # 占用钉 ↔ 腾空谓词全状态对账：
    #   未结态（充电中/结算中/已超时）必须钉住占用；终态（已关账/已中断）必须腾空。
    released_state = se["status"] in ("已关账", "已中断")
    occupied_state = se["status"] in ("充电中", "结算中", "已超时")
    drifts = []
    if abs(float(se["kwh"]) - replayed_kwh) > 1e-9:
        drifts.append("累计电量与脉冲行重算不一致")
    # 已关账金额须等于重算金额；在充会话金额是预估值，也按同价核对
    if abs(float(se["amount"]) - replayed_amount) > 1e-9:
        drifts.append("金额与电量×电价重算不一致")
    if released_state and pile and pile["status"] != "空闲":
        drifts.append(f"{se['status']}但桩位未释放")
    if released_state and gun and gun["status"] != "可用":
        drifts.append(f"{se['status']}但枪线未释放")
    if occupied_state and pile and pile["status"] != "占用":
        drifts.append("未结会话但桩位未占用（半截：占用与状态机分裂）")
    if occupied_state and gun and gun["status"] != "占用":
        drifts.append("未结会话但枪线未占用（半截：占用与状态机分裂）")
    if released_state and open_n != 0:
        drifts.append(f"{se['status']}仍存在未收口在途脉冲")
    if int(se["inflight_count"]) != open_n:
        drifts.append("会话在途计数与登记表不一致")
    # 回执成功却无对应电量行：每个“完成”登记必须有 pulse_row_id 且行存在
    orphan_done = 0
    done_regs = conn.execute(
        "SELECT * FROM inflight_pulses WHERE session_id=? AND status=?",
        (session_id, INFLIGHT_DONE),
    ).fetchall()
    for r in done_regs:
        exists = conn.execute(
            "SELECT 1 FROM pulses WHERE id=?", (r["pulse_row_id"],)
        ).fetchone()
        if not exists:
            orphan_done += 1
            drifts.append(f"在途登记{r['token'][:8]}标记完成却无对应电量行")
    return {
        "会话编号": session_id,
        "状态": se["status"],
        "脉冲行数": pulse_rows["n"],
        "重算电量": replayed_kwh,
        "账面电量": float(se["kwh"]),
        "重算金额": replayed_amount,
        "账面金额": float(se["amount"]),
        "在途未收口": open_n,
        "无电量行的完成登记": orphan_done,
        "桩位状态": pile["status"] if pile else None,
        "枪线状态": gun["status"] if gun else None,
        "一致": not drifts,
        "偏差": drifts,
    }


def repair_session(session_id: int) -> dict:
    """按重放真值修复半截状态，回滚到争用结局的一致快照。"""
    conn = connect(writer=True)
    try:
        with immediate_tx(conn):
            sweep_stale_inflight(conn, session_id)
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not se:
                raise HTTPException(404, detail="会话不存在")
            before = replay_session(conn, session_id)
            ts = now_str()
            # 修复“回执成功却无电量行”：把无对应 pulses 行的“完成”登记改判超时，
            # 使其不再声称成功；真值以现存 pulses 行为准。
            orphan = conn.execute(
                """SELECT i.* FROM inflight_pulses i
                   WHERE i.session_id=? AND i.status=?
                     AND NOT EXISTS(SELECT 1 FROM pulses p WHERE p.id=i.pulse_row_id)""",
                (session_id, INFLIGHT_DONE),
            ).fetchall()
            for r in orphan:
                conn.execute(
                    "UPDATE inflight_pulses SET status=?, completed_at=?, note=? WHERE id=?",
                    (INFLIGHT_TIMEOUT, ts, "修复：无对应电量行，改判超时未入账", r["id"]),
                )
            pulse_rows = conn.execute(
                "SELECT COALESCE(SUM(delta_kwh),0) AS k FROM pulses WHERE session_id=?",
                (session_id,),
            ).fetchone()
            true_kwh = round(float(pulse_rows["k"]), 3)
            price = _price_of(conn, se)
            true_amount = round(true_kwh * price, 2)
            conn.execute(
                "UPDATE sessions SET kwh=?, amount=?, inflight_count=? WHERE id=?",
                (true_kwh, true_amount,
                 _open_inflight_count(conn, session_id), session_id),
            )
            # 占用钉 ↔ 腾空谓词同命运修复：终态强制腾空，未结态强制钉住占用
            if se["status"] in ("已关账", "已中断"):
                conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
                conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
            elif se["status"] in ("充电中", "结算中", "已超时"):
                conn.execute("UPDATE piles SET status='占用' WHERE id=?", (se["pile_id"],))
                conn.execute("UPDATE guns SET status='占用' WHERE id=?", (se["gun_id"],))
            conn.execute(
                "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
                (session_id, "争用修复", ts,
                 f"按脉冲行重算电量{true_kwh}/金额{true_amount}，对齐状态机与占用"),
            )
            after = replay_session(conn, session_id)
            return {"修复前": before, "修复后": after}
    finally:
        conn.close()
