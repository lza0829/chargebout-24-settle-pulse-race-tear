from __future__ import annotations
from fastapi import HTTPException

from app.db import connect, immediate_tx
from app.services import contention
from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"锁定电价{price}"),
    )
    return {"会话号": slip, "会话编号": sid, "锁定电价": price, "状态": "充电中"}


def add_pulse(session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    # 口径B：在途脉冲优先。登记→落账两段式，全部串行化与重放逻辑在 contention 内。
    return contention.record_pulse(session_id, delta_kwh, power_kw)


def settle(session_id: int) -> dict:
    # 口径B：关账让在途脉冲先收口。屏障→等收口→临界提交，无半截、可重放。
    return contention.settle(session_id)


def mark_timeout(conn, session_id: int) -> dict:
    with immediate_tx(conn):
        se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not se:
            raise HTTPException(404, detail="会话不存在")
        if se["status"] != "充电中":
            raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
        # 口径B：超时入口同样先让崩溃残留的在途登记有着落（超时清扫），再核对在途
        contention.sweep_stale_inflight(conn, session_id)
        open_n = contention._open_inflight_count(conn, session_id)
        if open_n != 0:
            raise HTTPException(409, detail=f"仍有{open_n}笔在途脉冲未收口，不可标超时")
        minutes = int(get_setting(conn, "超时分钟", "120"))
        ts = now_str()
        conn.execute(
            "UPDATE sessions SET status='已超时', note=? WHERE id=?",
            (f"超过{minutes}分钟未关", session_id),
        )
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (session_id, "超时", ts, f"阈值{minutes}分钟"),
        )
        return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    with immediate_tx(conn):
        se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not se:
            raise HTTPException(404, detail="会话不存在")
        if se["status"] not in ("充电中", "结算中", "已超时"):
            raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
        # 口径B：中断也要先让在途登记有着落（超时清扫），避免无主在途
        contention.sweep_stale_inflight(conn, session_id)
        open_n = contention._open_inflight_count(conn, session_id)
        if open_n != 0:
            raise HTTPException(409, detail=f"仍有{open_n}笔在途脉冲未收口，不可中断")
        ts = now_str()
        conn.execute(
            "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
            (ts, reason, session_id),
        )
        conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
        conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (session_id, "异常中断", ts, reason),
        )
        return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()
    if open_se and status in ("故障", "维护"):
        # 底座默认：置故障/维护时若仍有在充，拒绝（后续题可改互斥）
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}

