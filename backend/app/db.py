from __future__ import annotations
import sqlite3
import time
from contextlib import contextmanager
from app.config import DB_PATH, WRITE_BUSY_TIMEOUT_SECONDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
-- 在途脉冲登记表（口径B）：脉冲先登记再落账，关账据此判断是否还有未收口在途
CREATE TABLE IF NOT EXISTS inflight_pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL UNIQUE,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT '在途',   -- 在途 | 完成 | 超时
  registered_at TEXT NOT NULL,
  settle_generation INTEGER NOT NULL DEFAULT 0,
  completed_at TEXT,
  pulse_row_id INTEGER,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
-- 争用结局快照/审计表：关账临界提交前留存“争用前快照”，半截崩溃可据此回滚/重放
CREATE TABLE IF NOT EXISTS contention_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  phase TEXT NOT NULL,                  -- 关账屏障 | 关账临界
  snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL,
  replayed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(writer: bool = False) -> sqlite3.Connection:
    """打开连接。

    writer=True 时设 isolation_level=None（autocommit 模式），事务完全由
    immediate_tx 显式 BEGIN/COMMIT/ROLLBACK 掌控，杜绝驱动隐式事务在
    BEGIN IMMEDIATE 前先取 SHARED 锁引发的锁升级死锁。
    """
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=WRITE_BUSY_TIMEOUT_SECONDS,
        isolation_level=None if writer else "",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # busy_timeout 让普通写锁争用等待而不是立刻抛 database is locked
    conn.execute(f"PRAGMA busy_timeout={int(WRITE_BUSY_TIMEOUT_SECONDS * 1000)}")
    return conn


def enable_wal() -> None:
    """启用 WAL（持久数据库属性，只需设置一次）。

    WAL 下写者不阻塞读者、写者之间排队，且消除回滚日志模式下
    SHARED→RESERVED 锁升级导致的死锁式 SQLITE_BUSY。失败则退回默认日志模式，
    由 immediate_tx 的重试兜底，不阻断启动。
    """
    conn = connect(writer=True)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """立即进入写事务并取写锁，保证脉冲/关账临界区严格串行化。

    BEGIN IMMEDIATE 即取得 RESERVED 锁，第二个写者排队，把“先查后写”变成
    可线性化的临界区。对锁升级死锁式 SQLITE_BUSY（busy_timeout 不等待的那种）
    做有界退避重试；BEGIN 尚未成功、无任何副作用，重试安全。
    """
    deadline = time.monotonic() + WRITE_BUSY_TIMEOUT_SECONDS
    delay = 0.005
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> None:
    """对既有库做幂等加列迁移（老库没有争用世代字段时补齐）。

    同时收口“假关账”旁路残留：surface_last/surface_ok 涂改总览的假绿键、
    settle_surface:* 旁路关账字一律清除——真值只认 sessions 状态机与 pulses 行，
    旁路键不删则旧库重启后仍可能读到涂改面。
    """
    cols = _column_names(conn, "sessions")
    if "settle_generation" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN settle_generation INTEGER NOT NULL DEFAULT 0")
    if "inflight_count" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN inflight_count INTEGER NOT NULL DEFAULT 0")
    conn.execute("DELETE FROM settings WHERE key IN ('surface_last', 'surface_ok')")
    conn.execute("DELETE FROM settings WHERE key LIKE 'settle_surface:%'")


def init_db() -> None:
    enable_wal()
    with db() as conn:
        conn.executescript(SCHEMA)
        migrate(conn)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
