"""新 GTD 本地存储：inbox（没时间的念头）+ daily_seq（当日序号）+ mirror（Reminder 镜像）。

重写后的架构：**有时间的任务全在 Reminder**，本地只留三样东西——

1. ``inbox``：还没定时间的念头。迁到 Reminder 后**行不删**，只写 ``moved_at`` /
   ``reminder_id``，Reminder 那边出问题还能查到原始记录。
2. ``daily_seq``：当天序号，可断号、不重复、只当天有效。
3. ``mirror``：Reminder 的本地副本，**可整表重建**（备份，不是真相源）。
4. ``board_index``：**最近一次出图的「图上号 → 条目引用」映射**（每次出图整批重写）。
   老板只记「图上的 3 号」，`done 3` 靠这张表反查是哪条；出图成功之后才写。

四张表同一个库文件（默认 ``data/gtd.db``）。本模块只管读写与事务：不打印、
不解析参数、不认识渲染格式。所有 ``now`` / ``day`` 都可注入，测试不依赖系统时钟。

另有一张内部辅助表 ``daily_seq_hwm``（当日序号高水位）：只为了「删掉行之后号也不
回收」，不对外，不属于上面三张业务表。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .priority import PRIORITY_NONE, normalize

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
  reminder_id TEXT,
  priority    TEXT NOT NULL DEFAULT 'none'   -- none|low|medium|high，与 Reminders.app 同口径
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

-- 图上的号 → 条目。**当天不变**（幂等）：已有行原样保留，新条目只有 INSERT，
-- 消失的条目才 DELETE；号一律 max(display_seq)+1。跨天按新的一天重新分配。
-- 列名照老板给的 schema 叫 reminder_id，内容沿用全局 ref 口径：
-- ``reminder:<uuid>`` 或 ``inbox:<行 id>``。
CREATE TABLE IF NOT EXISTS board_index (
  day         TEXT    NOT NULL,
  display_seq INTEGER NOT NULL,
  reminder_id TEXT    NOT NULL,
  PRIMARY KEY (day, display_seq)
);
"""

MIRROR_COLS = ("reminder_id", "title", "list_name", "due_at", "completed", "synced_at")

# 建表后校验列：旧库（v1）的 daily_seq 是 day/seq/task_id，撞名但不兼容，
# 不能靠 CREATE TABLE IF NOT EXISTS 静默留着——直接报错让人先迁移。
TABLE_COLUMNS = {
    "inbox": ("id", "title", "note", "created_at", "moved_at", "reminder_id", "priority"),
    "daily_seq": ("day", "seq", "ref"),
    "daily_seq_hwm": ("day", "last_seq"),
    "mirror": MIRROR_COLS,
    "board_index": ("day", "display_seq", "reminder_id"),
}

# 老库逐列补丁：`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，所以缺的列
# 只能 ALTER 补。先 PRAGMA 查一遍再改，重复跑 N 次也只改一次（幂等）。
MIGRATIONS = {
    "inbox": {
        "priority": "ALTER TABLE inbox ADD COLUMN priority TEXT NOT NULL DEFAULT 'none'",
    },
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


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列（如 inbox.priority）：缺什么补什么，补过就不再动。"""
    for table, columns in MIGRATIONS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, sql in columns.items():
            if column not in have:
                conn.execute(sql)


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
        _migrate(conn)
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

    def add(self, title, note="", *, now=None, priority: str = PRIORITY_NONE) -> int:
        """落一条念头；2 分钟内同标题视为同一条（幂等）。

        ``priority`` 跟 Reminders.app 同口径（none/low/medium/high）。重复命中已有行时
        不改标题，但如果这次说了「重要」，补标上去——老板重说一遍就是想让它重要。
        """
        at = _now(now)
        level = normalize(priority)
        dup = self._recent_duplicate(title, at)
        if dup is not None:
            if level != PRIORITY_NONE:
                self.set_priority(dup, level)
            return dup
        cur = self._db().execute(
            "INSERT INTO inbox (title, note, created_at, priority) VALUES (?, ?, ?, ?)",
            (title, note, _iso(at), level))
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
            "SELECT id, title, note, created_at, moved_at, reminder_id, priority FROM inbox "
            "WHERE moved_at IS NULL ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]

    def set_priority(self, inbox_id, value, *, now=None) -> bool:
        """改一条未迁出念头的优先级；已迁出（或没有这一行）返回 False。"""
        cur = self._db().execute(
            "UPDATE inbox SET priority=? WHERE id=? AND moved_at IS NULL",
            (normalize(value), inbox_id))
        return cur.rowcount > 0

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


# ---------------------------------------------------------------- board_index

class BoardIndex(_LocalTable):
    """图上号 → 条目。当天不变：已有号原样保留，新的只有 INSERT，消失的才 DELETE。

    幂等是硬要求：同样的数据反复出图，号一个字都不许变。所以这里**绝不整表重建**，
    也不读列表下标——新号唯一的来源是 ``next_number()``（``max(display_seq)+1``）。
    """

    def numbered(self, *, day=None) -> dict[str, int]:
        """当天已有的「条目引用 → 图上号」。"""
        rows = self._db().execute(
            "SELECT display_seq, reminder_id FROM board_index WHERE day=?",
            (_day_key(day),)).fetchall()
        return {row["reminder_id"]: row["display_seq"] for row in rows}

    def next_number(self, *, day=None) -> int:
        """下一个可用号：``max(display_seq)+1``（空表从 1 开始）。"""
        key = _day_key(day)
        row = self._db().execute(
            "SELECT COALESCE(MAX(display_seq), 0) AS top FROM board_index WHERE day=?",
            (key,)).fetchone()
        return int(row["top"]) + 1

    def lookup(self, number, *, day=None) -> str | None:
        """图上第 number 号 → 条目引用；这张图上没有这个号就 None。"""
        row = self._db().execute(
            "SELECT reminder_id FROM board_index WHERE day=? AND display_seq=?",
            (_day_key(day), number)).fetchone()
        return row["reminder_id"] if row is not None else None

    def sync(self, numbers: dict[str, int], *, day=None) -> int:
        """按 ``{引用: 号}`` 增量对齐当天的映射：保留已有、插入新增、删除消失的。

        已在表里的号**一个字段都不改**（不 UPDATE），所以重复调用是幂等的。
        """
        key = _day_key(day)
        conn = self._db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            known = {row["reminder_id"] for row in conn.execute(
                "SELECT reminder_id FROM board_index WHERE day=?", (key,))}
            gone = known - set(numbers)
            fresh = [(key, number, ref) for ref, number in numbers.items()
                     if ref not in known]
            conn.executemany(
                "DELETE FROM board_index WHERE day=? AND reminder_id=?", [(key, ref) for ref in gone])
            conn.executemany(
                "INSERT INTO board_index (day, display_seq, reminder_id) VALUES (?, ?, ?)", fresh)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return len(numbers)

    def count(self, *, day=None) -> int:
        row = self._db().execute(
            "SELECT COUNT(*) AS n FROM board_index WHERE day=?", (_day_key(day),)).fetchone()
        return int(row["n"])
