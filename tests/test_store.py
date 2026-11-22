"""阶段 1 · 数据层测试（先写测试，后写实现）。

覆盖：三条硬约束、幂等、序号锁定、capture 防丢、写库重试、undo、stats。
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from gtd.store import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    ConstraintError,
    NotFoundError,
    ParamError,
    Store,
    WriteError,
    fingerprint,
    kind_of,
    prod_db_path,
)


@pytest.fixture()
def store(tmp_path):
    """单元测试一律内存库：每次全新、零残留，永不落盘。

    capture_path 仍指 tmp_path —— 否则测试原话会写进真实 capture.jsonl。
    """
    s = Store(":memory:", tmp_path / "capture.jsonl")
    s.init()
    print(f"[unit-fixture] store.db_path = {s.db_path!r}")
    return s


# ---------- 建库 ----------

def test_init_is_idempotent(store):
    store.init()
    store.init()
    assert store.meta_get("schema_version") == "2"
    names = {r["name"] for r in store.query("select name from sqlite_master where type='table'")}
    assert {"tasks", "meta", "daily_seq", "tasks_archive"} <= names


def test_memory_db_has_memory_journal(store):
    """内存库不支持 WAL，journal_mode 返回 memory 才是正确行为。"""
    assert store.db_path == ":memory:"
    assert store.journal_mode() == "memory"


def test_file_db_keeps_wal(tmp_path):
    """文件库（含测试库）仍走 WAL。"""
    s = Store(tmp_path / "file.db", tmp_path / "capture.jsonl")
    s.init()
    assert s.journal_mode() == "wal"
    s.close()


# ---------- 正式库守卫 ----------

def test_pytest_refuses_prod_db(tmp_path, monkeypatch):
    """在 pytest 里构造正式库 → 立刻 RuntimeError，绝不静默写入。"""
    monkeypatch.delenv("GTD_DB", raising=False)
    with pytest.raises(RuntimeError) as exc:
        Store(prod_db_path(), tmp_path / "capture.jsonl")
    assert "测试不允许连正式库" in str(exc.value)


def test_default_store_under_pytest_is_also_refused(tmp_path, monkeypatch):
    """不传参走默认路径时，同样会被守卫拦下。"""
    monkeypatch.delenv("GTD_DB", raising=False)
    with pytest.raises(RuntimeError):
        Store(None, tmp_path / "capture.jsonl")


def test_guard_does_not_block_test_db(tmp_path, monkeypatch):
    """守卫只认正式库路径：GTD_DB 指到别处照样放行。"""
    monkeypatch.setenv("GTD_DB", str(tmp_path / "gtd.db"))
    s = Store(tmp_path / "gtd.db", tmp_path / "capture.jsonl")
    assert s.db_kind() == "test"


def test_db_kind_classification(tmp_path):
    assert kind_of(":memory:") == "memory"
    assert kind_of(tmp_path / "x.db") == "test"
    assert kind_of(prod_db_path()) == "prod"


def test_tasks_have_reserved_sync_columns(store):
    """列集合精确等于 DESIGN.md 第四节 —— 多一列少一列都算错。"""
    expected = {"id", "title", "capture_id", "status", "category", "due", "slot",
                "waiting_on", "defer", "note", "idem_key", "created_at", "updated_at",
                "completed_at", "external_id", "external_kind", "sync_state",
                "last_synced_at", "repeat_days", "done_count"}
    cols = {r["name"] for r in store.query("PRAGMA table_info(tasks)")}
    assert cols == expected


# ---------- 三条硬约束 ----------

def test_default_add_lands_in_inbox(store):
    res = store.add("买鸡蛋")
    assert res["ok"] is True and res["duplicate"] is False
    assert store.get(res["id"])["status"] == "inbox"
    assert store.get(res["id"])["category"] == "其他"
    assert store.get(res["id"])["slot"] == "all"


def test_add_with_due_lands_in_next(store):
    res = store.add("买鸡蛋", due="2026-11-23")
    assert store.get(res["id"])["status"] == "next"


def test_constraint_next_requires_date(store):
    tid = store.add("写周报", due="2026-11-23")["id"]
    with pytest.raises(ConstraintError):
        store.edit(tid, due="")


def test_constraint_waiting_on_requires_due(store):
    with pytest.raises(ConstraintError):
        store.add("等小王回邮件", defer="2026-11-30", waiting_on="小王")


def test_constraint_inbox_must_not_carry_schedule(store):
    tid = store.add("随手记")["id"]
    with pytest.raises(ConstraintError):
        store.edit(tid, due="2026-12-01")


# ---------- 参数校验 ----------

def test_bad_category_rejected(store):
    with pytest.raises(ParamError):
        store.add("x", category="不存在的分类")


def test_bad_slot_rejected(store):
    with pytest.raises(ParamError):
        store.add("x", due="2026-11-23", slot="25:00")


def test_bad_date_rejected(store):
    with pytest.raises(ParamError):
        store.add("x", due="2026-13-01")


def test_empty_title_rejected(store):
    with pytest.raises(ParamError):
        store.add("   ")


# ---------- 幂等 ----------

def test_duplicate_add_returns_same_id(store):
    a = store.add("买鸡蛋", due="2026-11-23")
    b = store.add("买鸡蛋", due="2026-11-23")
    assert b["duplicate"] is True and b["id"] == a["id"]
    assert store.count_tasks() == 1


def test_dup_ok_creates_new_record(store):
    a = store.add("买鸡蛋", due="2026-11-23")
    b = store.add("买鸡蛋", due="2026-11-23", dup_ok=True)
    assert b["duplicate"] is False and b["id"] != a["id"]
    assert store.count_tasks() == 2


def test_fingerprint_changes_with_category(store):
    assert fingerprint("a", "2026-11-23", "all", "工作", "next") != fingerprint(
        "a", "2026-11-23", "all", "投资", "next")


def test_complete_twice_keeps_completed_at(store):
    tid = store.add("倒垃圾", due="2026-11-22")["id"]
    first = store.complete([tid])
    before = store.get(tid)["completed_at"]
    time.sleep(0.01)
    second = store.complete([tid])
    assert first["duplicate"] is False and second["duplicate"] is True
    assert store.get(tid)["completed_at"] == before


# ---------- 序号 ----------

def test_seq_allocated_in_order(store):
    a = store.add("一", due="2026-11-22")["id"]
    b = store.add("二", due="2026-11-22")["id"]
    assert (store.seq_of(a), store.seq_of(b)) == (1, 2)


def test_seq_stable_across_status_change(store):
    tid = store.add("开会", due="2026-11-22")["id"]
    seq = store.seq_of(tid)
    store.complete([tid])
    assert store.seq_of(tid) == seq
    assert store.resolve(str(seq))["id"] == tid


def test_resolve_falls_back_to_global_id(store):
    tid = store.add("旧账", due="2026-11-22", dup_ok=True)["id"]
    assert store.resolve(str(tid))["id"] == tid
    with pytest.raises(NotFoundError):
        store.require("99999")


# ---------- capture 防丢 ----------

def test_capture_line_written_before_insert(store, monkeypatch):
    def boom(row):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_insert_task", boom)
    with pytest.raises(WriteError):
        store.add("别丢了我")
    lines = store.capture_lines()
    assert len(lines) == 1
    assert lines[0]["raw"] == "别丢了我"
    assert lines[0]["capture_id"]
    assert lines[0]["ts"]


def test_capture_id_links_to_task(store):
    tid = store.add("有回指")["id"]
    cap = store.get(tid)["capture_id"]
    assert cap in {line["capture_id"] for line in store.capture_lines()}


def test_write_retries_twice_then_succeeds(store, monkeypatch):
    calls = {"n": 0}
    original = store._insert_task

    def flaky(row):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return original(row)

    monkeypatch.setattr(store, "_insert_task", flaky)
    res = store.add("重试三次")
    assert res["ok"] is True
    assert calls["n"] == 3
    assert store.stats()["write_failures"] == 0


def test_write_failure_counts_and_reports(store, monkeypatch):
    def boom(row):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_insert_task", boom)
    with pytest.raises(WriteError) as exc:
        store.add("必失败")
    assert exc.value.capture_id
    assert store.stats()["write_failures"] == 1


# ---------- 条目流与统计 ----------

def test_entries_are_view_agnostic(store):
    store.add("甲", due="2026-11-20")
    store.add("乙")
    entries = store.entries(day="2026-11-22")
    assert {e["kind"] for e in entries} == {"task"}
    assert {"kind", "id", "seq", "title", "slot", "category", "tags", "status", "due"} <= set(entries[0])
    assert any("逾期" in e["tags"] for e in entries)


def test_stats_shape(store):
    store.add("逾期项", due="2026-11-01")
    store.add("收件箱项")
    st = store.stats()
    assert set(st) == {"overdue", "done_today", "inbox", "inbox_oldest_days",
                       "uncategorized_ratio", "write_failures"}
    assert st["overdue"] == 1 and st["inbox"] == 1


def test_doctor_reports_broken_rows(store):
    tid = store.add("被写坏", due="2026-11-22")["id"]
    store.query("pragma writable_schema=on")  # 绕过约束直接破坏，模拟人工误改
    store.conn.execute("update tasks set due=null where id=?", (tid,))
    report = store.doctor()
    assert report["ok"] is False
    kinds = {p["kind"] for p in report["problems"]}
    assert "next_without_date" in kinds


def test_undo_removes_added_task(store):
    tid = store.add("手滑")["id"]
    res = store.undo()
    assert res["action"] == "add" and tid in res["ids"]
    assert store.count_tasks() == 0
    with pytest.raises(NotFoundError):
        store.require(tid)


def test_undo_restores_status(store):
    tid = store.add("误点完成", due="2026-11-22")["id"]
    store.complete([tid])
    store.undo()
    assert store.get(tid)["status"] == "next"
    assert store.get(tid)["completed_at"] is None


def test_undo_empty_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.undo()


def test_categories_are_eight_plus_other():
    assert CATEGORIES == ("购物", "投资", "家庭", "健康", "家务", "学习", "工作", "预约", "其他")
    assert DEFAULT_CATEGORY == "其他"

# ================================================================
# 周期任务（repeat_days）：先写测试，后写实现。
# 规则：从**今天**起算往后推 N 天，不是从原到期日起算。
# ================================================================

REPEAT_DAY = "2026-11-22"
REPEAT_NEXT = "2026-11-27"

# 冻结「今天」，让「推到哪天」是字面量而不是心里算的
@pytest.fixture()
def frozen_today(monkeypatch):
    monkeypatch.setattr("gtd.store.today_str", lambda: REPEAT_DAY)
    return REPEAT_DAY


def _repeat_task(store, days=5, due=REPEAT_DAY):
    return store.add("拼多多领券", due=due, repeat_days=days,
                     category="购物", dup_ok=True)["id"]


# ---------- 完成 → 滚到下一个到期日 ----------

def test_repeat_done_rolls_due_from_today(store, frozen_today):
    """due = today + 5；原 due 是逾期也不影响起算基准。"""
    tid = _repeat_task(store, due="2026-11-20")
    res = store.complete([tid])
    assert store.get(tid)["due"] == REPEAT_NEXT
    assert res["repeat"] == {"days": 5, "next_due": REPEAT_NEXT, "done_count": 1}


def test_repeat_keeps_status_next(store, frozen_today):
    tid = _repeat_task(store)
    store.complete([tid])
    assert store.get(tid)["status"] == "next"


def test_repeat_done_count_increments(store, frozen_today):
    tid = _repeat_task(store)
    store.complete([tid])
    assert store.get(tid)["done_count"] == 1
    # 模拟下一个周期（把上次完成时刻挪到 5 天前）再完成一次
    store.query("update tasks set completed_at=? where id=?",
                ("2026-11-17T09:00:00+08:00", tid))
    res = store.complete([tid])
    assert store.get(tid)["done_count"] == 2
    assert res["repeat"]["done_count"] == 2


def test_repeat_done_twice_same_day_does_not_double_roll(store, frozen_today):
    """同一天连点两次 done：不许把 due 再推 5 天、done_count 也不许再涨。"""
    tid = _repeat_task(store)
    store.complete([tid])
    second = store.complete([tid])
    assert store.get(tid)["due"] == REPEAT_NEXT
    assert store.get(tid)["done_count"] == 1
    assert second["duplicate"] is True


def test_oneoff_done_unchanged(store, frozen_today):
    """一次性任务的行为没被改坏：status=done、due 不动、没有 repeat 字段。"""
    tid = store.add("倒垃圾", due=REPEAT_DAY)["id"]
    res = store.complete([tid])
    row = store.get(tid)
    assert (row["status"], row["due"], row["done_count"]) == ("done", REPEAT_DAY, 0)
    assert "repeat" not in res and "repeats" not in res


# ---------- undo 三样一起回滚 ----------

def test_repeat_undo_restores_all_three(store, frozen_today):
    tid = _repeat_task(store)
    before = dict(store.get(tid))
    store.complete([tid])
    after = dict(store.get(tid))
    assert (after["due"], after["done_count"], after["completed_at"]) != (
        before["due"], before["done_count"], before["completed_at"])

    payload = _last_undo_payload(store)
    assert (payload["prev_due"], payload["prev_done_count"], payload["prev_status"]) == (
        before["due"], before["done_count"], before["status"])

    res = store.undo()
    row = dict(store.get(tid))
    assert res["action"] == "update"
    for key in ("due", "done_count", "completed_at", "status"):
        assert row[key] == before[key], key


def _last_undo_payload(store) -> dict:
    row = store.query("select payload from undo_log order by id desc limit 1")[0]
    return json.loads(row["payload"])


# ---------- add / edit 的参数校验 ----------

def test_add_repeat_without_due_rejected(store):
    with pytest.raises(ParamError):
        store.add("领券", repeat_days=5)


def test_repeat_days_range(store):
    for bad in (0, -1, 366, "abc"):
        with pytest.raises(ParamError):
            store.add("领券", due=REPEAT_DAY, repeat_days=bad)
    assert store.add("下限", due=REPEAT_DAY, repeat_days=1, dup_ok=True)["ok"]
    assert store.add("上限", due=REPEAT_DAY, repeat_days=365, dup_ok=True)["ok"]


def test_edit_sets_and_cancels_repeat_days(store, frozen_today):
    tid = store.add("领券", due=REPEAT_DAY)["id"]
    store.edit(tid, repeat_days=5)
    assert store.get(tid)["repeat_days"] == 5
    store.edit(tid, repeat_days="0")
    assert store.get(tid)["repeat_days"] is None
    store.edit(tid, repeat_days=7)
    store.edit(tid, repeat_days="")
    assert store.get(tid)["repeat_days"] is None


def test_edit_repeat_on_inbox_rejected(store):
    tid = store.add("随手记")["id"]
    with pytest.raises(ParamError):
        store.edit(tid, repeat_days=5)


def test_repeat_not_in_fingerprint(store, frozen_today):
    """边界：指纹不含 repeat_days —— 同一件事改了周期仍视为同一条。"""
    first = store.add("领券", due=REPEAT_DAY, repeat_days=5)
    second = store.add("领券", due=REPEAT_DAY)
    assert second["duplicate"] is True and second["id"] == first["id"]


# ---------- 迁移 v1 → v2 ----------

V1_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, capture_id TEXT,
    status TEXT NOT NULL DEFAULT 'inbox'
           CHECK (status IN ('inbox','next','done','dropped')),
    category TEXT NOT NULL DEFAULT '其他', due TEXT,
    slot TEXT NOT NULL DEFAULT 'all', waiting_on TEXT, defer TEXT, note TEXT,
    idem_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    completed_at TEXT, external_id TEXT, external_kind TEXT,
    sync_state TEXT NOT NULL DEFAULT 'none'
           CHECK (sync_state IN ('none','pending','synced','failed')),
    last_synced_at TEXT
);
CREATE TABLE tasks_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, capture_id TEXT,
    status TEXT NOT NULL DEFAULT 'inbox'
           CHECK (status IN ('inbox','next','done','dropped')),
    category TEXT NOT NULL DEFAULT '其他', due TEXT,
    slot TEXT NOT NULL DEFAULT 'all', waiting_on TEXT, defer TEXT, note TEXT,
    idem_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    completed_at TEXT, external_id TEXT, external_kind TEXT,
    sync_state TEXT NOT NULL DEFAULT 'none'
           CHECK (sync_state IN ('none','pending','synced','failed')),
    last_synced_at TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE daily_seq (day TEXT NOT NULL, seq INTEGER NOT NULL, task_id INTEGER NOT NULL,
    UNIQUE (day, seq), UNIQUE (day, task_id));
CREATE TABLE undo_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    action TEXT NOT NULL, payload TEXT NOT NULL);
INSERT INTO meta(key, value) VALUES ('schema_version', '1');
INSERT INTO tasks(title, status, category, due, slot, created_at, updated_at)
    VALUES ('老库里的任务', 'next', '购物', '2026-11-22', 'am',
            '2026-11-01T09:00:00+08:00', '2026-11-01T09:00:00+08:00');
"""


def test_migration_from_v1(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.commit()
    conn.close()

    s = Store(path, tmp_path / "capture.jsonl")
    report = s.init()
    assert report["schema_version"] == "2"
    assert s.meta_get("schema_version") == "2"
    for table in ("tasks", "tasks_archive"):
        cols = {r["name"] for r in s.query(f"PRAGMA table_info({table})")}
        assert {"repeat_days", "done_count"} <= cols, table

    row = s.get(1)
    assert (row["title"], row["due"], row["slot"]) == ("老库里的任务", "2026-11-22", "am")
    assert row["repeat_days"] is None and row["done_count"] == 0

    # 幂等：再 init 两次，不报错、不重复加列、数据不丢
    s.init()
    s.init()
    assert s.count_tasks() == 1
    assert s.db_path == path
    s.close()
