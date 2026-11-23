"""新 GTD 本地存储：inbox（没时间的念头）+ daily_seq（当日序号）+ mirror（Reminder 镜像）。

重写后的架构：**有时间的任务全在 Reminder**，本地只留三样东西——

1. ``inbox``：还没定时间的念头。迁到 Reminder 后**行不删**，只写 ``moved_at`` /
   ``reminder_id``，Reminder 那边出问题还能查到原始记录。
2. ``daily_seq``：当天序号，可断号、不重复、只当天有效。
3. ``mirror``：Reminder 的本地副本，**可整表重建**（备份，不是真相源）。

三张表同一个库文件（默认 ``data/gtd.db``）。本模块只管读写与事务：不打印、
不解析参数、不认识渲染格式。所有 ``now`` / ``day`` 都可注入，测试不依赖系统时钟。

另有一张内部辅助表 ``daily_seq_hwm``（当日序号高水位）：只为了「删掉行之后号也不
回收」，不对外，不属于上面三张业务表。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "gtd.db"

IDEM_WINDOW = timedelta(minutes=2)   # add 的幂等窗口
BUSY_TIMEOUT_MS = 5000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  moved_at    TEXT,
  reminder_id TEXT
);

CREATE TABLE IF NOT EXISTS daily_seq (
  day  TEXT NOT NULL,
  seq  INTEGER NOT NULL,
  ref  TEXT NOT NULL,
  PRIMARY KEY (day, seq)
);

-- 内部表：当日序号高水位。daily_seq 的号「不回收」靠它——
-- 只删行（不删号）时 max(seq) 会下降，高水位保证下一个号仍是上一个号 +1。
CREATE TABLE IF NOT EXISTS daily_seq_hwm (
  day      TEXT NOT NULL PRIMARY KEY,
  last_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mirror (
  reminder_id TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  list_name   TEXT NOT NULL,
  due_at      TEXT,
  completed   INTEGER NOT NULL DEFAULT 0,
  synced_at   TEXT NOT NULL
);
"""

MIRROR_COLS = ("reminder_id", "title", "list_name", "due_at", "completed", "synced_at")

# 建表后校验列：旧库（v1）的 daily_seq 是 day/seq/task_id，撞名但不兼容，
# 不能靠 CREATE TABLE IF NOT EXISTS 静默留着——直接报错让人先迁移。
TABLE_COLUMNS = {
    "inbox": ("id", "title", "note", "created_at", "moved_at", "reminder_id"),
    "daily_seq": ("day", "seq", "ref"),
    "daily_seq_hwm": ("day", "last_seq"),
    "mirror": MIRROR_COLS,
}


# ---------------------------------------------------------------- 纯函数

def _now(now: datetime | None = None) -> datetime:
    """唯一时钟入口；默认本地时间（无时区，与「ISO 本地时间」口径一致）。"""
    return now if now is not None else datetime.now()


def _iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _day_key(day=None) -> str:
    """把 None / date / datetime / 'YYYY-MM-DD' 统一成当天键。"""
    if day is None:
        return date.today().isoformat()
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    return str(day)


def _check_schema(conn: sqlite3.Connection) -> None:
    """列不齐就报错：旧库同名的 daily_seq 不能静默顶着新结构用。"""
    for table, needed in TABLE_COLUMNS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = [col for col in needed if col not in have]
        if missing:
            raise RuntimeError(
                f"{table} 表结构不兼容，缺列 {missing}（现为 {sorted(have)}）。"
                f"旧库 data/gtd.db 的 daily_seq 是 v1 的 day/seq/task_id 结构，"
                f"重写后的新结构要 day/seq/ref——先把旧库迁移或重建，再跑新代码。")


# ---------------------------------------------------------------- 连接基类

class _LocalTable:
    """三张表共用一个库文件：每个实例自己持有连接，走 WAL + busy_timeout。"""

    def __init__(self, db_path=None):
        raw = DEFAULT_DB if db_path is None else db_path
        self.is_memory = str(raw) == ":memory:"
        self.db_path: Path | str = ":memory:" if self.is_memory else Path(raw)
        self.conn: sqlite3.Connection | None = None
        self._refuse_prod_under_test()

    def _refuse_prod_under_test(self) -> None:
        """硬守卫：pytest 里连正式库直接炸，绝不静默写入。"""
        if not os.environ.get("PYTEST_CURRENT_TEST") or self.is_memory:
            return
        if Path(self.db_path).resolve() != DEFAULT_DB.resolve():
            return
        raise RuntimeError(
            f"测试不允许连正式库：{self.db_path}。单测用 ':memory:'，"
            f"需要跨实例共享时用 tmp_path 临时文件")

    def _connect(self) -> sqlite3.Connection:
        if not self.is_memory:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if not self.is_memory:          # 内存库不支持 WAL，只对文件库设
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.executescript(SCHEMA_SQL)
        _check_schema(conn)
        return conn

    def _db(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = self._connect()
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self._db()
        return self

    def __exit__(self, *exc_info):
        self.close()


# ---------------------------------------------------------------- inbox

class Inbox(_LocalTable):
    """没时间的念头。迁出 = 打标记，不删行。"""

    def add(self, title, note="", *, now=None) -> int:
        at = _now(now)
        dup = self._recent_duplicate(title, at)
        if dup is not None:
            return dup
        cur = self._db().execute(
            "INSERT INTO inbox (title, note, created_at) VALUES (?, ?, ?)",
            (title, note, _iso(at)))
        new_id = cur.lastrowid
        assert new_id is not None
        return new_id

    def _recent_duplicate(self, title: str, at: datetime) -> int | None:
        """同标题、未迁出、created_at 在 2 分钟内 → 已存在的 id。"""
        row = self._db().execute(
            "SELECT id, created_at FROM inbox "
            "WHERE title=? AND moved_at IS NULL ORDER BY id DESC LIMIT 1",
            (title,)).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        return row["id"] if abs(at - created) <= IDEM_WINDOW else None

    def pending(self) -> list[dict]:
        rows = self._db().execute(
            "SELECT id, title, note, created_at, moved_at, reminder_id FROM inbox "
            "WHERE moved_at IS NULL ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]

    def mark_moved(self, inbox_id, reminder_id, *, now=None) -> bool:
        """写 moved_at + reminder_id；已是迁出态的行不再改（返回 False）。"""
        cur = self._db().execute(
            "UPDATE inbox SET moved_at=?, reminder_id=? "
            "WHERE id=? AND moved_at IS NULL",
            (_iso(_now(now)), reminder_id, inbox_id))
        return cur.rowcount > 0

    def oldest_age_days(self, *, now=None) -> int | None:
        """最老一条未迁出念头的自然日龄；空表 None。"""
        row = self._db().execute(
            "SELECT MIN(created_at) AS oldest FROM inbox WHERE moved_at IS NULL").fetchone()
        if row["oldest"] is None:
            return None
        created = datetime.fromisoformat(row["oldest"]).date()
        return (_now(now).date() - created).days


# ---------------------------------------------------------------- daily_seq

class Seq(_LocalTable):
    """当天序号：只 max+1，断号不补，已分配的号不回收。"""

    def assign(self, ref, *, day=None) -> int:
        """当天 max(seq)+1，再与高水位取大：删掉行也不会把号放回来。"""
        key = _day_key(day)
        conn = self._db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COALESCE((SELECT last_seq FROM daily_seq_hwm WHERE day=?), 0) AS hwm, "
                "COALESCE((SELECT MAX(seq) FROM daily_seq WHERE day=?), 0) AS top",
                (key, key)).fetchone()
            nxt = max(row["hwm"], row["top"]) + 1
            conn.execute("INSERT INTO daily_seq (day, seq, ref) VALUES (?, ?, ?)",
                         (key, nxt, ref))
            conn.execute(
                "INSERT INTO daily_seq_hwm (day, last_seq) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET last_seq=excluded.last_seq", (key, nxt))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return nxt

    def lookup(self, seq, *, day=None) -> str | None:
        row = self._db().execute(
            "SELECT ref FROM daily_seq WHERE day=? AND seq=?",
            (_day_key(day), seq)).fetchone()
        return row["ref"] if row is not None else None

    def all(self, *, day=None) -> dict[int, str]:
        rows = self._db().execute(
            "SELECT seq, ref FROM daily_seq WHERE day=? ORDER BY seq",
            (_day_key(day),)).fetchall()
        return {row["seq"]: row["ref"] for row in rows}


# ---------------------------------------------------------------- mirror

class Mirror(_LocalTable):
    """Reminder 的本地副本：整表重建，事务内 DELETE + INSERT。"""

    def replace_all(self, rows, *, now=None) -> int:
        params = [self._load(row, now) for row in rows]
        conn = self._db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM mirror")
            conn.executemany(
                "INSERT INTO mirror (reminder_id, title, list_name, due_at, "
                "completed, synced_at) VALUES (?, ?, ?, ?, ?, ?)", params)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return len(params)

    @staticmethod
    def _load(row, now) -> tuple:
        data = dict(row)
        return (
            data["reminder_id"], data["title"], data["list_name"],
            data.get("due_at"),
            int(data.get("completed") or 0),
            data.get("synced_at") or _iso(_now(now)),
        )

    def all(self) -> list[dict]:
        rows = self._db().execute(
            "SELECT reminder_id, title, list_name, due_at, completed, synced_at "
            "FROM mirror ORDER BY reminder_id").fetchall()
        return [dict(row) for row in rows]
