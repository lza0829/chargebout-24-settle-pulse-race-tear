from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db
from app.services import contention, core
from app.services import sessions as svc

router = APIRouter()


class OpenIn(BaseModel):
    桩位编号: int
    枪线编号: int | None = None


class PulseIn(BaseModel):
    电量: float = Field(gt=0)
    功率: float = 0


class AbortIn(BaseModel):
    原因: str = "急停"


@router.get("/会话")
def list_sessions(状态: str | None = None):
    with db() as conn:
        return core.list_sessions(conn, 状态)


@router.get("/会话/{session_id}")
def get_session(session_id: int):
    with db() as conn:
        d = core.session_detail(conn, session_id)
        if not d:
            raise HTTPException(404, detail="会话不存在")
        d["重放核对"] = contention.replay_session(conn, session_id)
        return d


@router.post("/开会话")
def open_session(body: OpenIn):
    with db() as conn:
        return svc.open_session(conn, body.桩位编号, body.枪线编号)


@router.post("/会话/{session_id}/脉冲")
def add_pulse(session_id: int, body: PulseIn):
    # 自管连接的两段式争用协议（登记→落账），不与外层 db() 事务混用
    return svc.add_pulse(session_id, body.电量, body.功率)


@router.post("/会话/{session_id}/结算")
def settle(session_id: int):
    # 自管连接的两段式关账（屏障→收口→临界提交）
    return svc.settle(session_id)


@router.get("/会话/{session_id}/在途")
def inflight(session_id: int):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise HTTPException(404, detail="会话不存在")
        return core.list_inflight(conn, session_id)


@router.get("/会话/{session_id}/争用快照")
def journal(session_id: int):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise HTTPException(404, detail="会话不存在")
        return core.list_journal(conn, session_id)


@router.get("/会话/{session_id}/重放")
def replay(session_id: int):
    with db() as conn:
        r = contention.replay_session(conn, session_id)
        if r is None:
            raise HTTPException(404, detail="会话不存在")
        # 标记快照已被重放（审计），不改变业务真值
        conn.execute(
            "UPDATE contention_journal SET replayed_at=? WHERE session_id=? AND replayed_at IS NULL",
            (core.now_str(), session_id),
        )
        return r


@router.post("/会话/{session_id}/修复")
def repair(session_id: int):
    return contention.repair_session(session_id)


@router.post("/会话/{session_id}/超时")
def timeout(session_id: int):
    with db() as conn:
        return svc.mark_timeout(conn, session_id)


@router.post("/会话/{session_id}/中断")
def abort(session_id: int, body: AbortIn | None = None):
    with db() as conn:
        reason = body.原因 if body else "急停"
        return svc.abort_session(conn, session_id, reason)

