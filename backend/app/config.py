from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "充桩条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CHARGEBOUT_DB", str(ROOT / "data" / "chargebout.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

PILE_STATUSES = ("空闲", "占用", "故障", "维护")
GUN_STATUSES = ("可用", "占用", "故障")
SESSION_STATUSES = ("充电中", "结算中", "已关账", "已超时", "已中断")
OPEN_SESSIONS = ("充电中", "结算中")

# ── 争用口径（全局唯一，禁止与“关账优先”混用）────────────────────────────
# 口径B：在途脉冲优先，完成后再关。
#   1) 脉冲先在 inflight_pulses 登记（可见），再进入临界区落账；已登记的在途脉冲必落账。
#   2) 关账先把会话推进“结算中”关账屏障（世代+1）：屏障之后新登记的脉冲整笔拒绝；
#      屏障之前已登记的在途脉冲必须全部收口（完成或超时）后才允许提交关账。
#   3) 关账完成前若仍有在途脉冲，总览如实展示在途数，绝不假装零在途。
#   4) 关账态、桩枪占用、累计电量、金额、详情、总览在同一事务同刷。
CONTENTION_POLICY = "在途脉冲优先"
# 单条在途脉冲登记的最长存活时间，超过即视为崩溃残留，由收口清扫标记为“超时”
INFLIGHT_TIMEOUT_SECONDS = float(os.environ.get("CHARGEBOUT_INFLIGHT_TIMEOUT", "30"))
# 关账等待在途脉冲收口的最长时间；超时则关账整笔失败（会话停留“结算中”，可重试，不留半截）
SETTLE_DRAIN_TIMEOUT_SECONDS = float(os.environ.get("CHARGEBOUT_SETTLE_DRAIN_TIMEOUT", "3"))
SETTLE_DRAIN_POLL_SECONDS = float(os.environ.get("CHARGEBOUT_SETTLE_DRAIN_POLL", "0.05"))
# 写事务拿不到写锁时的等待上限
WRITE_BUSY_TIMEOUT_SECONDS = float(os.environ.get("CHARGEBOUT_BUSY_TIMEOUT", "5"))
