"""GTD 数据层：SQLite 真相源、三条硬约束、幂等、序号、capture 日志。

职责边界（S）：本模块只管数据与约束。
- 不打印任何东西（返回值交给命令层）
- 不解析命令行参数
- 不认识渲染格式

本模块是全项目唯一 import sqlite3 的地方（D：依赖倒置）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATUSES = ("inbox", "next", "done", "dropped")
OPEN_STATUSES = ("inbox", "next")
CATEGORIES = ("购物", "投资", "家庭", "健康", "家务", "学习", "工作", "预约", "其他")
DEFAULT_CATEGORY = "其他"
SLOT_RE = re.compile(r"^(all|am|pm|(?:before|at):([01]\d|2[0-3]):([0-5]\d))$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA_VERSION = "1"
IDEM_WINDOW_SECONDS = 120
RETRY_DELAYS = (0.2, 0.5)
CAPTURE_ROTATE_BYTES = 10 * 1024 * 1024
ARCHIVE_AFTER_DAYS = 90

_UNSET = object()


# ---------------------------------------------------------------- 异常

class GtdError(Exception):
    """项目内所有可预期错误的基类。"""


class ParamError(GtdError, ValueError):
    """参数不合法（退出码 1）。"""


class ConstraintError(GtdError, ValueError):
    """违反三条硬约束（退出码 1）。"""


class NotFoundError(GtdError, LookupError):
    """找不到记录 / 没有可撤销操作（退出码 2）。"""


class WriteError(GtdError):
    """写库重试两次仍失败（退出码 2，原话已在 capture.jsonl）。"""

    def __init__(self, message: str, capture_id: str | None = None):
        super().__init__(message)
        self.capture_id = capture_id


# ---------------------------------------------------------------- 纯函数

def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def today_str() -> str:
    return now().date().isoformat()


def fingerprint(title: str, due: str | None, slot: str, category: str, status: str) -> str:
    """内容指纹：sha256(title|due|slot|category|status)[:16]。"""
    raw = "|".join([title, due or "", slot, category, status])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def prod_db_path() -> Path:
    """正式库位置：固定，不受 GTD_DB 影响 —— 测试守卫比的就是它。"""
    return PROJECT_ROOT / "data" / "gtd.db"


def integration_db_path() -> Path:
    """集成测试专用库（可随时清空）：GTD_TEST_DB 或 data/test/gtd.db。

    名字不叫 test_* —— 免得被 import 进测试模块后 pytest 当成用例收集。
    """
    return Path(os.environ.get("GTD_TEST_DB") or PROJECT_ROOT / "data" / "test" / "gtd.db")


def default_db_path() -> Path:
    return Path(os.environ.get("GTD_DB") or prod_db_path())


def kind_of(db_path) -> str:
    """库身份：prod / test / memory。纯按路径判，不打开库。"""
    if str(db_path) == ":memory:":
        return "memory"
    return "prod" if Path(db_path).resolve() == prod_db_path().resolve() else "test"


def default_capture_path() -> Path:
    return Path(os.environ.get("GTD_CAPTURE") or PROJECT_ROOT / "capture.jsonl")


def validate_task(status: str, due: str | None, defer: str | None,
                  waiting_on: str | None) -> None:
    """三条硬约束。违反即抛 ConstraintError，不做任何修补。"""
    problems = []
    if status == "next" and not due and not defer:
        problems.append("硬约束1：status=next 时 due 或 defer 至少一个非空")
    if waiting_on and not due:
        problems.append("硬约束2：waiting_on 非空时 due 必须非空")
    if status == "inbox" and (due or defer):
        problems.append("硬约束3：status=inbox 时不得携带 due/defer（请先 clarify）")
    if problems:
        raise ConstraintError("；".join(problems))


def _clean_title(title) -> str:
    text = (title or "").strip()
    if not text:
        raise ParamError("标题不能为空")
    return text


def _opt_date(value, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not DATE_RE.match(text):
        raise ParamError(f"{field} 必须是 YYYY-MM-DD：{value!r}")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ParamError(f"{field} 不是合法日期：{value!r}") from exc
    return text


def _clean_slot(slot) -> str:
    text = (slot or "all").strip()
    if not SLOT_RE.match(text):
        raise ParamError(f"slot 非法：{slot!r}（all/am/pm/before:HH:MM/at:HH:MM）")
    return text


def _clean_category(category) -> str:
    text = (category or DEFAULT_CATEGORY).strip()
    if text not in CATEGORIES:
        raise ParamError(f"category 非法：{category!r}（可选：{'/'.join(CATEGORIES)}）")
    return text


def slot_time(slot: str) -> str | None:
    """排序用时刻：有明确时刻返回 HH:MM，am/pm 折算，all 返回 None。"""
    if slot == "am":
        return "12:00"
    if slot == "pm":
        return "18:00"
    if ":" in slot:
        return slot.split(":", 1)[1]
    return None


def _age_seconds(stamp: str | None) -> float:
    if not stamp:
        return 1e9
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return 1e9
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return (now() - parsed).total_seconds()


def _days_since(stamp: str | None, today: str) -> int:
    if not stamp:
        return 0
    try:
        return max(0, (datetime.fromisoformat(today).date()
                       - datetime.fromisoformat(stamp[:10]).date()).days)
    except ValueError:
        return 0


# ---------------------------------------------------------------- 建表 SQL

TASK_COLUMNS_SQL = """
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    capture_id     TEXT,
    status         TEXT    NOT NULL DEFAULT 'inbox'
                   CHECK (status IN ('inbox','next','done','dropped')),
    category       TEXT    NOT NULL DEFAULT '其他',
    due            TEXT,
    slot           TEXT    NOT NULL DEFAULT 'all',
    waiting_on     TEXT,
    defer          TEXT,
    note           TEXT,
    idem_key       TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    completed_at   TEXT,
    external_id    TEXT,
    external_kind  TEXT,
    sync_state     TEXT    NOT NULL DEFAULT 'none'
                   CHECK (sync_state IN ('none','pending','synced','failed')),
    last_synced_at TEXT
"""

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS tasks ({TASK_COLUMNS_SQL});
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_due    ON tasks(due);
CREATE INDEX IF NOT EXISTS idx_tasks_idem   ON tasks(idem_key);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS daily_seq (
    day     TEXT    NOT NULL,
    seq     INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    UNIQUE (day, seq),
    UNIQUE (day, task_id)
);
CREATE TABLE IF NOT EXISTS tasks_archive ({TASK_COLUMNS_SQL});
CREATE TABLE IF NOT EXISTS undo_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    action  TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

DOCTOR_CHECKS = (
    ("next_without_date",
     "select id from tasks where status='next' and coalesce(due,'')='' and coalesce(defer,'')=''"),
    ("waiting_without_due",
     "select id from tasks where coalesce(waiting_on,'')!='' and coalesce(due,'')=''"),
    ("inbox_with_schedule",
     "select id from tasks where status='inbox' and (coalesce(due,'')!='' or coalesce(defer,'')!='')"),
    ("bad_status",
     "select id from tasks where status not in ('inbox','next','done','dropped')"),
    ("bad_category",
     f"select id from tasks where category not in ({','.join('?' * len(CATEGORIES))})"),
    ("bad_slot",
     "select id from tasks where slot not in ('all','am','pm')"
     " and slot not like 'before:%' and slot not like 'at:%'"),
    ("orphan_seq",
     "select task_id as id from daily_seq where task_id not in (select id from tasks)"),
    ("done_without_completed_at",
     "select id from tasks where status='done' and coalesce(completed_at,'')=''"),
)


# ---------------------------------------------------------------- Store

class Store:
    """SQLite 数据层门面。所有写操作走 _write()，带二次重试。"""

    def __init__(self, db_path=None, capture_path=None):
        raw = db_path or default_db_path()
        self.is_memory = str(raw) == ":memory:"
        self.db_path = ":memory:" if self.is_memory else Path(raw)
        self.capture_path = Path(capture_path or default_capture_path())
        self.conn: sqlite3.Connection | None = None
        self._refuse_prod_under_test()

    def _refuse_prod_under_test(self) -> None:
        """硬守卫：pytest 里连正式库直接炸，绝不静默写入。

        比的是固定的 prod_db_path()，所以 GTD_DB / GTD_TEST_DB 指到别处
        既不会误伤集成测试，也关不掉这道闸。
        """
        if not os.environ.get("PYTEST_CURRENT_TEST") or self.is_memory:
            return
        if Path(self.db_path).resolve() != prod_db_path().resolve():
            return
        raise RuntimeError(
            f"测试不允许连正式库：{self.db_path}。单测用 Store(':memory:')，"
            f"集成测试用 GTD_TEST_DB 指向 data/test/gtd.db")

    # ---- 生命周期 ----

    def connect(self) -> sqlite3.Connection:
        if not self.is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if not self.is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self.conn = conn
        return conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self.init()
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _db(self) -> sqlite3.Connection:
        return self.conn if self.conn is not None else self.connect()

    def init(self) -> dict:
        """建表 + 写 schema_version。重复执行安全。"""
        conn = self._db()
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (SCHEMA_VERSION,))
        return {"ok": True, "db": str(self.db_path), "capture": str(self.capture_path),
                "schema_version": SCHEMA_VERSION, "wal": self.journal_mode()}

    def journal_mode(self) -> str:
        return self._db().execute("PRAGMA journal_mode").fetchone()[0].lower()

    def db_kind(self) -> str:
        """库身份：prod / test / memory（doctor 与看板上下文都用它）。"""
        return kind_of(self.db_path)

    # ---- 低层读取 ----

    def query(self, sql: str, params=()):
        return self._db().execute(sql, params).fetchall()

    def meta_get(self, key: str, default=None):
        row = self._db().execute("select value from meta where key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self._db().execute(
            "insert into meta(key, value) values(?, ?) "
            "on conflict(key) do update set value=excluded.value", (key, str(value)))

    def count_tasks(self) -> int:
        return self._db().execute("select count(*) from tasks").fetchone()[0]

    def get(self, task_id) -> sqlite3.Row:
        row = self._db().execute("select * from tasks where id=?", (int(task_id),)).fetchone()
        if row is None:
            raise NotFoundError(f"找不到 id={task_id} 的记录")
        return row

    def resolve(self, ref, day: str | None = None) -> sqlite3.Row | None:
        """先按当日 daily_seq 解析，再退回全局 id。"""
        day = day or today_str()
        try:
            number = int(str(ref).lstrip("#"))
        except ValueError as exc:
            raise ParamError(f"id 必须是数字：{ref!r}") from exc
        hit = self._db().execute(
            "select task_id from daily_seq where day=? and seq=?", (day, number)).fetchone()
        if hit is not None:
            return self._db().execute("select * from tasks where id=?",
                                      (hit["task_id"],)).fetchone()
        return self._db().execute("select * from tasks where id=?", (number,)).fetchone()

    def require(self, ref, day: str | None = None) -> sqlite3.Row:
        row = self.resolve(ref, day)
        if row is None:
            raise NotFoundError(f"找不到记录：{ref}")
        return row

    def seq_of(self, task_id: int, day: str | None = None) -> int | None:
        day = day or today_str()
        row = self._db().execute("select seq from daily_seq where day=? and task_id=?",
                                 (day, int(task_id))).fetchone()
        return int(row["seq"]) if row else None

    def public(self, row) -> dict:
        return dict(row)

    # ---- 写操作基础设施 ----

    def _tx(self, body):
        conn = self._db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = body()
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _write(self, action, capture_id: str | None = None):
        """执行写事务；OperationalError 重试两次（200ms / 500ms）。"""
        last: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return action()
            except sqlite3.OperationalError as exc:
                last = exc
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
        self._bump_write_failures()
        raise WriteError(f"写库失败（已重试 {len(RETRY_DELAYS)} 次）：{last}",
                         capture_id=capture_id)

    def _bump_write_failures(self) -> None:
        key = f"write_failures:{today_str()}"
        try:
            self.meta_set(key, str(int(self.meta_get(key, "0") or 0) + 1))
        except sqlite3.Error:
            pass

    def _capture_dir(self) -> Path:
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)
        return self.capture_path.parent

    def _rotate_capture(self) -> None:
        if not self.capture_path.exists():
            return
        if self.capture_path.stat().st_size <= CAPTURE_ROTATE_BYTES:
            return
        target = self._capture_dir() / f"capture-{now().strftime('%Y%m')}.jsonl"
        if target.exists():
            target = target.with_name(f"capture-{now().strftime('%Y%m')}-{secrets.token_hex(2)}.jsonl")
        self.capture_path.rename(target)

    def capture(self, raw: str, extra: dict | None = None) -> dict:
        """写库之前先落 capture.jsonl（含 ts / raw / capture_id）。"""
        self._rotate_capture()
        capture_id = f"{now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
        line = {"ts": now_iso(), "raw": raw, "capture_id": capture_id, **(extra or {})}
        with open(self.capture_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return line

    def capture_lines(self) -> list[dict]:
        if not self.capture_path.exists():
            return []
        out = []
        for text in self.capture_path.read_text(encoding="utf-8").splitlines():
            if text.strip():
                try:
                    out.append(json.loads(text))
                except json.JSONDecodeError:
                    continue
        return out

    def _push_undo(self, action: str, payload: dict) -> None:
        self._db().execute("insert into undo_log(ts, action, payload) values(?,?,?)",
                           (now_iso(), action, json.dumps(payload, ensure_ascii=False)))

    # ---- 插入与更新 ----

    def _insert_task(self, row: dict) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        cur = self._db().execute(f"insert into tasks ({cols}) values ({marks})",
                                 tuple(row.values()))
        return int(cur.lastrowid)

    def _alloc_seq(self, day: str, task_id: int) -> int:
        row = self._db().execute(
            "select coalesce(max(seq), 0) + 1 as n from daily_seq where day=?", (day,)).fetchone()
        seq = int(row["n"])
        self._db().execute("insert into daily_seq(day, seq, task_id) values (?,?,?)",
                           (day, seq, task_id))
        return seq

    def _insert_new(self, row: dict) -> int:
        task_id = self._insert_task(row)
        self._alloc_seq(today_str(), task_id)
        self._push_undo("add", {"ids": [task_id]})
        return task_id

    def _row_snapshot(self, task_id: int) -> dict:
        return dict(self.get(task_id))

    def _update_fields(self, task_id: int, values: dict) -> None:
        before = self._row_snapshot(task_id)
        cols = ", ".join(f"{key}=?" for key in values)
        self._db().execute(f"update tasks set {cols}, updated_at=? where id=?",
                           (*values.values(), now_iso(), task_id))
        self._push_undo("update", {"before": [self._public_scalar(before)]})

    @staticmethod
    def _public_scalar(snapshot: dict) -> dict:
        return {k: v for k, v in snapshot.items() if k != "id"} | {"id": snapshot["id"]}

    def _recent_duplicate(self, idem: str):
        row = self._db().execute(
            "select * from tasks where idem_key=? and status!='dropped' order by id desc limit 1",
            (idem,)).fetchone()
        if row is None or _age_seconds(row["created_at"]) > IDEM_WINDOW_SECONDS:
            return None
        return row

    def changed_of(self, task_id: int) -> dict:
        row = self.get(task_id)
        keys = ("title", "status", "due", "slot", "category", "waiting_on", "defer", "note")
        return {k: row[k] for k in keys}

    def _result(self, task_id: int, duplicate: bool, changed: dict) -> dict:
        return {"ok": True, "id": task_id, "duplicate": duplicate,
                "changed": changed, "stats": self.stats()}

    # ---- 命令层入口：写 ----

    def add(self, title, due=None, slot=None, category=None, waiting_on=None,
            defer=None, note=None, dup_ok: bool = False) -> dict:
        """捕获一条任务。有 due/defer → next，否则 inbox。"""
        title = _clean_title(title)
        due_v, defer_v = _opt_date(due, "due"), _opt_date(defer, "defer")
        slot_v, category_v = _clean_slot(slot), _clean_category(category)
        waiting_v = (waiting_on or "").strip() or None
        status = "next" if (due_v or defer_v) else "inbox"
        validate_task(status, due_v, defer_v, waiting_v)
        idem = fingerprint(title, due_v, slot_v, category_v, status)
        line = self.capture(title, {"cmd": "add", "due": due_v, "defer": defer_v,
                                    "slot": slot_v, "category": category_v,
                                    "waiting_on": waiting_v})
        if not dup_ok:
            dup = self._recent_duplicate(idem)
            if dup is not None:
                return self._result(dup["id"], True, self.changed_of(dup["id"]))
        row = {"title": title, "status": status, "due": due_v, "defer": defer_v,
               "slot": slot_v, "category": category_v, "waiting_on": waiting_v,
               "note": note, "idem_key": idem, "capture_id": line["capture_id"],
               "created_at": now_iso(), "updated_at": now_iso()}
        task_id = self._write(lambda: self._tx(lambda: self._insert_new(row)),
                              capture_id=line["capture_id"])
        return self._result(task_id, False, self.changed_of(task_id))

    def clarify(self, ref, action, due=None, slot=None, category=None,
                note=None, day: str | None = None) -> dict:
        """收件箱 → 下一步：标题换成具体动作，默认今天可做。"""
        row = self.require(ref, day)
        merged = {
            "title": _clean_title(action),
            "status": "next",
            "due": _opt_date(due, "due") if due is not None else row["due"],
            "defer": row["defer"],
            "waiting_on": row["waiting_on"],
            "slot": _clean_slot(slot) if slot else row["slot"],
            "category": _clean_category(category) if category else row["category"],
            "note": note if note is not None else row["note"],
        }
        if not merged["due"] and not merged["defer"]:
            merged["due"] = today_str()
        validate_task(merged["status"], merged["due"], merged["defer"], merged["waiting_on"])
        self._write(lambda: self._tx(lambda: self._update_fields(row["id"], merged)))
        return self._result(row["id"], False, merged)

    def edit(self, ref, day: str | None = None, **fields) -> dict:
        """改内容字段；绝不允许改 status。"""
        allowed = {"title", "due", "defer", "slot", "category", "note", "waiting_on"}
        if "status" in fields:
            raise ParamError("edit 不允许改 status，请用 clarify / done / drop")
        unknown = set(fields) - allowed
        if unknown:
            raise ParamError(f"edit 不支持字段：{sorted(unknown)}")
        row = self.require(ref, day)
        merged = self._merge_fields(row, fields)
        validate_task(row["status"], merged["due"], merged["defer"], merged["waiting_on"])
        self._write(lambda: self._tx(lambda: self._update_fields(row["id"], merged)))
        return self._result(row["id"], False, merged)

    def _merge_fields(self, row, fields: dict) -> dict:
        merged = {k: row[k] for k in ("title", "due", "defer", "slot", "category",
                                      "note", "waiting_on")}
        if "title" in fields:
            merged["title"] = _clean_title(fields["title"])
        if "due" in fields:
            merged["due"] = _opt_date(fields["due"], "due")
        if "defer" in fields:
            merged["defer"] = _opt_date(fields["defer"], "defer")
        if "slot" in fields:
            merged["slot"] = _clean_slot(fields["slot"])
        if "category" in fields:
            merged["category"] = _clean_category(fields["category"])
        if "note" in fields:
            merged["note"] = fields["note"]
        if "waiting_on" in fields:
            merged["waiting_on"] = (fields["waiting_on"] or "").strip() or None
        return merged

    def complete(self, refs, day: str | None = None) -> dict:
        """done：可多个；已完成的不动 completed_at。"""
        rows = [self.require(ref, day) for ref in self._as_list(refs)]
        todo = [r["id"] for r in rows if r["status"] != "done"]
        if todo:
            self._write(lambda: self._tx(lambda: self._set_status(todo, "done", stamp=True)))
        return self._bulk_result([r["id"] for r in rows], duplicate=not todo,
                                 changed={"status": "done"})

    def drop(self, refs, day: str | None = None) -> dict:
        rows = [self.require(ref, day) for ref in self._as_list(refs)]
        todo = [r["id"] for r in rows if r["status"] != "dropped"]
        if todo:
            self._write(lambda: self._tx(lambda: self._set_status(todo, "dropped")))
        return self._bulk_result([r["id"] for r in rows], duplicate=not todo,
                                 changed={"status": "dropped"})

    def _as_list(self, refs) -> list:
        items = refs if isinstance(refs, (list, tuple)) else [refs]
        if not items:
            raise ParamError("至少给一个 id")
        return list(items)

    def _bulk_result(self, ids: list[int], duplicate: bool, changed: dict) -> dict:
        return {"ok": True, "id": ids[0] if len(ids) == 1 else None, "ids": ids,
                "duplicate": duplicate, "changed": changed, "stats": self.stats()}

    def _set_status(self, ids: list[int], status: str, stamp: bool = False) -> int:
        ts = now_iso()
        for task_id in ids:
            before = self._public_scalar(self._row_snapshot(task_id))
            if stamp:
                self._db().execute(
                    "update tasks set status=?, completed_at=coalesce(completed_at, ?),"
                    " updated_at=? where id=?", (status, ts, ts, task_id))
            else:
                self._db().execute("update tasks set status=?, updated_at=? where id=?",
                                   (status, ts, task_id))
            self._push_undo("update", {"before": [before]})
        return len(ids)

    # ---- undo ----

    def undo(self) -> dict:
        row = self._db().execute("select * from undo_log order by id desc limit 1").fetchone()
        if row is None:
            raise NotFoundError("没有可撤销的操作")
        payload = json.loads(row["payload"])
        ids = self._write(lambda: self._tx(
            lambda: self._apply_undo(row["id"], row["action"], payload)))
        return {"ok": True, "action": row["action"], "ids": ids, "stats": self.stats()}

    def _apply_undo(self, log_id: int, action: str, payload: dict) -> list[int]:
        conn = self._db()
        conn.execute("delete from undo_log where id=?", (log_id,))
        if action == "add":
            for task_id in payload["ids"]:
                conn.execute("delete from daily_seq where task_id=?", (task_id,))
                conn.execute("delete from tasks where id=?", (task_id,))
            return list(payload["ids"])
        ids = []
        for snap in payload.get("before", []):
            ids.append(snap["id"])
            values = {k: v for k, v in snap.items() if k != "id"}
            cols = ", ".join(f"{key}=?" for key in values)
            conn.execute(f"update tasks set {cols} where id=?", (*values.values(), snap["id"]))
        return ids

    # ---- 统计 / 诊断 ----

    def stats(self) -> dict:
        conn, day = self._db(), today_str()
        open_n = conn.execute(
            "select count(*) from tasks where status not in ('done','dropped')").fetchone()[0]
        other_n = conn.execute(
            "select count(*) from tasks where status not in ('done','dropped')"
            " and category='其他'").fetchone()[0]
        oldest = conn.execute(
            "select min(created_at) from tasks where status='inbox'").fetchone()[0]
        return {
            "overdue": conn.execute(
                "select count(*) from tasks where status not in ('done','dropped')"
                " and coalesce(due,'')!='' and due<?", (day,)).fetchone()[0],
            "done_today": conn.execute(
                "select count(*) from tasks where completed_at like ?",
                (day + "%",)).fetchone()[0],
            "inbox": conn.execute(
                "select count(*) from tasks where status='inbox'").fetchone()[0],
            "inbox_oldest_days": _days_since(oldest, day),
            "uncategorized_ratio": round(other_n / open_n, 2) if open_n else 0.0,
            "write_failures": int(self.meta_get(f"write_failures:{day}", "0") or 0),
        }

    def doctor(self) -> dict:
        """只读体检：能报出被人工写坏的记录。"""
        problems = []
        for kind, sql in DOCTOR_CHECKS:
            params = CATEGORIES if kind == "bad_category" else ()
            rows = self._db().execute(sql, params).fetchall()
            if rows:
                problems.append({"kind": kind, "count": len(rows),
                                 "sample_ids": [r[0] for r in rows[:5]]})
        return {"ok": not problems, "problems": problems, "counts": self._doctor_counts(),
                "db": str(self.db_path), "db_kind": self.db_kind(),
                "journal_mode": self.journal_mode(),
                "capture_lines": len(self.capture_lines()),
                "capture_bytes": self.capture_path.stat().st_size if self.capture_path.exists() else 0}

    def _doctor_counts(self) -> dict:
        conn = self._db()
        return {status: conn.execute("select count(*) from tasks where status=?",
                                     (status,)).fetchone()[0] for status in STATUSES}

    # ---- 备份 / 归档 ----

    def backup(self) -> dict:
        """当日有变化才产出 data/backup/gtd-YYYYMMDD.db。"""
        target_dir = self.db_path.parent / "backup"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"gtd-{now().strftime('%Y%m%d')}.db"
        sig = self._state_signature()
        if target.exists() and self.meta_get("backup_sig") == sig:
            return {"ok": True, "performed": False, "path": str(target), "reason": "当日无变化"}
        dst = sqlite3.connect(target)
        with dst:
            self._db().backup(dst)
        dst.close()
        self.meta_set("backup_sig", sig)
        return {"ok": True, "performed": True, "path": str(target), "signature": sig}

    def _state_signature(self) -> str:
        conn = self._db()
        row = conn.execute("select count(*) as n, coalesce(max(updated_at),'') as m from tasks").fetchone()
        return f"{row['n']}:{row['m']}"

    def archive_old_done(self, days: int = ARCHIVE_AFTER_DAYS) -> int:
        cutoff = (now().date().toordinal() - days)
        cutoff_day = datetime.fromordinal(cutoff).date().isoformat()
        rows = self._db().execute(
            "select * from tasks where status='done' and coalesce(completed_at,'')!=''"
            " and substr(completed_at,1,10) < ?", (cutoff_day,)).fetchall()
        if not rows:
            return 0
        cols = ", ".join(rows[0].keys())
        marks = ", ".join("?" * len(rows[0].keys()))
        for row in rows:
            self._db().execute(f"insert into tasks_archive ({cols}) values ({marks})", tuple(row))
            self._db().execute("delete from tasks where id=?", (row["id"],))
        return len(rows)

    # ---- 条目流（渲染层输入，唯一出口） ----

    def entries(self, day: str | None = None) -> list[dict]:
        """把库里的行转成「统一条目流」，渲染层只认这个结构。"""
        day = day or today_str()
        seqs = {r["task_id"]: r["seq"] for r in
                self._db().execute("select * from daily_seq where day=?", (day,))}
        rows = self._db().execute("select * from tasks order by id").fetchall()
        stream = [self._entry(row, seqs.get(row["id"]), day) for row in rows]
        return sorted(stream, key=_entry_sort)

    @staticmethod
    def _entry(row, seq, day: str) -> dict:
        tags = []
        open_task = row["status"] not in ("done", "dropped")
        if open_task and row["due"] and row["due"] < day:
            tags.append("逾期")
        if row["waiting_on"]:
            tags.append(f"等{row['waiting_on']}")
        return {"kind": "task", "id": row["id"], "seq": seq, "title": row["title"],
                "slot": row["slot"], "category": row["category"], "tags": tags,
                "due": row["due"], "defer": row["defer"], "status": row["status"],
                "waiting_on": row["waiting_on"], "note": row["note"],
                "capture_id": row["capture_id"], "completed_at": row["completed_at"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "sync_state": row["sync_state"]}


def _entry_sort(entry: dict):
    rank = {"next": 0, "inbox": 1, "done": 2, "dropped": 2}.get(entry["status"], 3)
    return (rank, entry["due"] or "9999-12-31", slot_time(entry["slot"]) or "99:99",
            entry["seq"] if entry["seq"] is not None else 10 ** 6)
