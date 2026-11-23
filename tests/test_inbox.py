"""inbox.py 的验收测试：全部注入固定 now，不依赖系统时钟。

固定基准：2026-11-23（周一）09:00。
单表用例一律 ':memory:'；需要跨实例共享同一个库时（三表共存 / 文件路径）用 tmp_path。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from gtd.inbox import DEFAULT_DB, BoardIndex, Inbox, Mirror, Seq

NOW = datetime(2026, 11, 23, 9, 0)
TODAY = date(2026, 11, 23)
YESTERDAY = date(2026, 11, 22)


def _row(reminder_id, title="x", list_name="下一步", due_at=None,
         completed=0, synced_at=None):
    return {
        "reminder_id": reminder_id,
        "title": title,
        "list_name": list_name,
        "due_at": due_at,
        "completed": completed,
        "synced_at": synced_at or NOW.isoformat(timespec="seconds"),
    }


@pytest.fixture
def inbox():
    box = Inbox(":memory:")
    yield box
    box.close()


@pytest.fixture
def seq():
    s = Seq(":memory:")
    yield s
    s.close()


@pytest.fixture
def mirror():
    m = Mirror(":memory:")
    yield m
    m.close()


# ---------- 1-3：add 的返回与幂等 ----------

def test_1_add_returns_id_and_pending_shows_it(inbox):
    new_id = inbox.add("给猫买罐头", now=NOW)
    assert isinstance(new_id, int) and new_id > 0
    pending = inbox.pending()
    assert [(r["id"], r["title"]) for r in pending] == [(new_id, "给猫买罐头")]


def test_2_same_title_within_2min_returns_same_id(inbox):
    first = inbox.add("给猫买罐头", now=NOW)
    again = inbox.add("给猫买罐头", now=NOW + timedelta(seconds=119))
    assert again == first
    assert len(inbox.pending()) == 1


def test_3_same_title_after_2min_returns_new_id(inbox):
    first = inbox.add("给猫买罐头", now=NOW)
    later = inbox.add("给猫买罐头", now=NOW + timedelta(minutes=3))
    assert later != first
    assert len(inbox.pending()) == 2


def test_3b_same_title_exactly_2min_still_deduped(inbox):
    first = inbox.add("上边界", now=NOW)
    assert inbox.add("上边界", now=NOW + timedelta(minutes=2)) == first


# ---------- 4-5：mark_moved 是打标记，不是删除 ----------

def test_4_mark_moved_hides_from_pending_but_keeps_row(inbox):
    new_id = inbox.add("写周报", now=NOW)
    assert inbox.mark_moved(new_id, "UUID-1", now=NOW) is True
    assert inbox.pending() == []
    row = inbox._db().execute(
        "SELECT title, moved_at, reminder_id FROM inbox WHERE id=?", (new_id,)).fetchone()
    assert row["title"] == "写周报"
    assert row["moved_at"] == NOW.isoformat(timespec="seconds")


def test_5_mark_moved_writes_reminder_id(inbox):
    new_id = inbox.add("写周报", now=NOW)
    inbox.mark_moved(new_id, "UUID-abc", now=NOW)
    row = inbox._db().execute(
        "SELECT reminder_id FROM inbox WHERE id=?", (new_id,)).fetchone()
    assert row["reminder_id"] == "UUID-abc"


def test_5b_mark_moved_twice_second_returns_false(inbox):
    new_id = inbox.add("写周报", now=NOW)
    assert inbox.mark_moved(new_id, "UUID-1", now=NOW) is True
    assert inbox.mark_moved(new_id, "UUID-2", now=NOW) is False
    row = inbox._db().execute(
        "SELECT reminder_id FROM inbox WHERE id=?", (new_id,)).fetchone()
    assert row["reminder_id"] == "UUID-1"


def test_5c_mark_moved_unknown_id_returns_false(inbox):
    assert inbox.mark_moved(999, "UUID-x", now=NOW) is False


def test_5d_moved_title_can_be_captured_again(inbox):
    """已迁出的旧标题再次抄进来，是新念头，不能被幂等吞掉。"""
    first = inbox.add("交房租", now=NOW)
    inbox.mark_moved(first, "UUID-1", now=NOW)
    second = inbox.add("交房租", now=NOW + timedelta(seconds=10))
    assert second != first
    assert [r["id"] for r in inbox.pending()] == [second]


# ---------- 6-7：oldest_age_days ----------

def test_6_oldest_age_days_empty_is_none(inbox):
    assert inbox.oldest_age_days(now=NOW) is None


def test_7_oldest_age_days_three_days_old(inbox):
    inbox.add("三天前记的", now=NOW - timedelta(days=3))
    inbox.add("今天记的", now=NOW)
    assert inbox.oldest_age_days(now=NOW) == 3


def test_7b_oldest_age_days_ignores_moved_rows(inbox):
    old = inbox.add("已经处理掉的老念头", now=NOW - timedelta(days=9))
    inbox.add("昨天记的", now=NOW - timedelta(days=1))
    inbox.mark_moved(old, "UUID-old", now=NOW)
    assert inbox.oldest_age_days(now=NOW) == 1


# ---------- 8-12：daily_seq ----------

def test_8_assign_first_is_1_then_2(seq):
    assert seq.assign("inbox:1", day=TODAY) == 1
    assert seq.assign("inbox:2", day=TODAY) == 2


def test_9_assign_does_not_fill_gaps(seq):
    seq.assign("inbox:1", day=TODAY)
    seq.assign("inbox:2", day=TODAY)
    seq._db().execute("DELETE FROM daily_seq WHERE seq=2 AND day=?", (TODAY.isoformat(),))
    assert seq.assign("inbox:3", day=TODAY) == 3


def test_10_lookup_returns_ref(seq):
    seq.assign("reminder:UUID-9", day=TODAY)
    assert seq.lookup(1, day=TODAY) == "reminder:UUID-9"


def test_11_lookup_missing_is_none(seq):
    seq.assign("inbox:1", day=TODAY)
    assert seq.lookup(2, day=TODAY) is None


def test_12_seq_restarts_each_day(seq):
    assert seq.assign("inbox:1", day=YESTERDAY) == 1
    assert seq.assign("inbox:2", day=YESTERDAY) == 2
    assert seq.assign("inbox:3", day=TODAY) == 1


def test_12b_all_returns_day_map(seq):
    seq.assign("inbox:1", day=TODAY)
    seq.assign("reminder:UUID-2", day=TODAY)
    seq.assign("inbox:9", day=YESTERDAY)
    assert seq.all(day=TODAY) == {1: "inbox:1", 2: "reminder:UUID-2"}
    assert seq.all(day=YESTERDAY) == {1: "inbox:9"}


# ---------- 13-16：mirror ----------

def test_13_replace_all_first_write(mirror):
    n = mirror.replace_all([_row("a"), _row("b"), _row("c")], now=NOW)
    assert n == 3
    assert [r["reminder_id"] for r in mirror.all()] == ["a", "b", "c"]


def test_14_replace_all_replaces_not_appends(mirror):
    mirror.replace_all([_row("a"), _row("b")], now=NOW)
    mirror.replace_all([_row("z")], now=NOW)
    assert [r["reminder_id"] for r in mirror.all()] == ["z"]


def test_15_replace_all_rolls_back_on_failure(mirror):
    mirror.replace_all([_row("a"), _row("b")], now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        mirror.replace_all([_row("c"), _row("d"), _row("c")], now=NOW)   # 批次内主键撞车
    assert [r["reminder_id"] for r in mirror.all()] == ["a", "b"]


def test_15b_failed_batch_leaves_no_partial_rows(mirror):
    mirror.replace_all([_row("a")], now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        mirror.replace_all([_row("b"), _row("c"), _row("b")], now=NOW)  # b 插完 c 再撞 b
    assert [r["reminder_id"] for r in mirror.all()] == ["a"]


def test_16_all_returns_complete_fields(mirror):
    mirror.replace_all([_row("a", title="买菜", list_name="购物",
                             due_at="2026-11-23T18:00:00", completed=True,
                             synced_at="2026-11-23T09:00:00")], now=NOW)
    assert mirror.all() == [{
        "reminder_id": "a", "title": "买菜", "list_name": "购物",
        "due_at": "2026-11-23T18:00:00", "completed": 1,
        "synced_at": "2026-11-23T09:00:00",
    }]


def test_16b_replace_all_fills_synced_at_from_injected_now(mirror):
    mirror.replace_all([{"reminder_id": "a", "title": "t", "list_name": "l"}], now=NOW)
    assert mirror.all()[0]["synced_at"] == NOW.isoformat(timespec="seconds")
    assert mirror.all()[0]["completed"] == 0


def test_16c_replace_all_empty_clears_table(mirror):
    mirror.replace_all([_row("a")], now=NOW)
    assert mirror.replace_all([], now=NOW) == 0
    assert mirror.all() == []


# ---------- 17-18：三表共存 / 两种库路径 ----------

def test_17_three_tables_coexist_in_one_file(tmp_path):
    db = tmp_path / "gtd.db"
    inbox, seq, mirror = Inbox(db), Seq(db), Mirror(db)
    new_id = inbox.add("还没定时间的念头", now=NOW)
    assert seq.assign(f"inbox:{new_id}", day=TODAY) == 1
    assert mirror.replace_all([_row("pm-1", title="已定时间")], now=NOW) == 1
    mirror.replace_all([_row("pm-2")], now=NOW)          # 整表重建不动别的表
    assert [r["title"] for r in inbox.pending()] == ["还没定时间的念头"]
    assert seq.lookup(1, day=TODAY) == f"inbox:{new_id}"
    assert [r["reminder_id"] for r in mirror.all()] == ["pm-2"]
    for obj in (inbox, seq, mirror):
        obj.close()


def test_18_memory_and_file_paths_both_work(tmp_path):
    mem = Inbox(":memory:")
    assert mem.add("内存库", now=NOW) == 1
    assert mem._db().execute("PRAGMA journal_mode").fetchone()[0].lower() == "memory"
    mem.close()

    file_box = Inbox(tmp_path / "gtd.db")
    assert file_box.add("文件库", now=NOW) == 1
    assert file_box._db().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    file_box.close()


def test_18b_pytest_refuses_real_prod_db():
    with pytest.raises(RuntimeError, match="测试不允许连正式库"):
        Inbox(DEFAULT_DB)


def test_19_old_v1_daily_seq_is_rejected_loudly(tmp_path):
    """正式库里已有一张 v1 的 daily_seq（day/seq/task_id），不能静默顶着用。"""
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE daily_seq (day TEXT, seq INTEGER, task_id INTEGER, "
                "UNIQUE(day, seq), UNIQUE(day, task_id))")
    raw.commit()
    raw.close()
    seq = Seq(db)
    with pytest.raises(RuntimeError, match="daily_seq 表结构不兼容"):
        seq.all(day=TODAY)


def test_19b_seq_never_regresses_after_rows_are_cleared(seq):
    seq.assign("inbox:1", day=TODAY)
    seq.assign("inbox:2", day=TODAY)
    seq._db().execute("DELETE FROM daily_seq WHERE day=?", (TODAY.isoformat(),))
    assert seq.assign("inbox:3", day=TODAY) == 3
    assert seq.all(day=TODAY) == {3: "inbox:3"}


# ---------- 20-24：board_index（图上号，当天幂等） ----------

@pytest.fixture
def board():
    b = BoardIndex(":memory:")
    yield b
    b.close()


def test_20_sync_writes_numbers_and_lookup_reads_back(board):
    board.sync({"reminder:a": 1, "inbox:7": 2}, day=TODAY)
    assert board.numbered(day=TODAY) == {"reminder:a": 1, "inbox:7": 2}
    assert board.lookup(2, day=TODAY) == "inbox:7"
    assert board.count(day=TODAY) == 2


def test_21_sync_twice_is_idempotent(board):
    numbers = {"reminder:a": 1, "reminder:b": 2}
    board.sync(numbers, day=TODAY)
    snapshot = board._db().execute(
        "SELECT day, display_seq, reminder_id FROM board_index ORDER BY display_seq").fetchall()
    board.sync(numbers, day=TODAY)
    again = board._db().execute(
        "SELECT day, display_seq, reminder_id FROM board_index ORDER BY display_seq").fetchall()
    assert [tuple(row) for row in again] == [tuple(row) for row in snapshot]


def test_22_next_number_is_max_plus_one(board):
    assert board.next_number(day=TODAY) == 1
    board.sync({"reminder:a": 1, "reminder:b": 4}, day=TODAY)
    assert board.next_number(day=TODAY) == 5


def test_23_sync_drops_gone_refs_and_keeps_the_rest(board):
    board.sync({"reminder:a": 1, "reminder:b": 2, "reminder:c": 3}, day=TODAY)
    board.sync({"reminder:a": 1, "reminder:c": 3}, day=TODAY)
    assert board.numbered(day=TODAY) == {"reminder:a": 1, "reminder:c": 3}
    assert board.lookup(2, day=TODAY) is None


def test_24_new_day_starts_from_one(board):
    board.sync({"reminder:a": 1, "reminder:b": 2}, day=YESTERDAY)
    assert board.next_number(day=TODAY) == 1
    board.sync({"reminder:c": 1}, day=TODAY)
    assert board.numbered(day=TODAY) == {"reminder:c": 1}
    assert board.numbered(day=YESTERDAY) == {"reminder:a": 1, "reminder:b": 2}, "昨天的图不受影响"


# ---------- 优先级：priority 列（与 Reminders.app 同口径的四档） ----------

def test_priority_defaults_to_none(inbox):
    new_id = inbox.add("给猫买罐头", now=NOW)
    assert new_id > 0
    assert inbox.pending()[0]["priority"] == "none"


def test_add_with_priority_keeps_it(inbox):
    inbox.add("给猫买罐头", now=NOW, priority="high")
    assert inbox.pending()[0]["priority"] == "high"


def test_add_unknown_priority_falls_back_to_none(inbox):
    """认不出的档位一律 none：宁可少标，不许瞎标。"""
    inbox.add("给猫买罐头", now=NOW, priority="urgent")
    assert inbox.pending()[0]["priority"] == "none"


def test_set_priority_updates_pending(inbox):
    new_id = inbox.add("给猫买罐头", now=NOW)
    assert inbox.set_priority(new_id, "medium") is True
    assert inbox.pending()[0]["priority"] == "medium"


def test_set_priority_refuses_moved_row(inbox):
    """已经迁出收件箱的行不再改优先级（它现在归 Reminder 管）。"""
    new_id = inbox.add("给猫买罐头", now=NOW)
    inbox.mark_moved(new_id, "reminder-1", now=NOW)
    assert inbox.set_priority(new_id, "high") is False


def test_readd_within_window_marks_existing_row_important(inbox):
    """2 分钟内同一句话重说一遍、这次带「重要」→ 补标到原来那一行，不新建。"""
    first = inbox.add("给猫买罐头", now=NOW)
    again = inbox.add("给猫买罐头", now=NOW + timedelta(seconds=30), priority="high")
    assert again == first
    assert inbox.pending()[0]["priority"] == "high"


def test_old_db_gets_priority_column_added(tmp_path):
    """1.1.0 的老库没有 inbox.priority：连上来先 ALTER 补列，老数据一条不丢。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE inbox (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, moved_at TEXT,"
        " reminder_id TEXT);"
        "INSERT INTO inbox (title, note, created_at)"
        " VALUES ('旧念头', '', '2026-11-20T09:00:00');")
    conn.commit()
    conn.close()
    box = Inbox(db)
    try:
        rows = box.pending()
        assert [(row["title"], row["priority"]) for row in rows] == [("旧念头", "none")]
        assert box.set_priority(rows[0]["id"], "high") is True
    finally:
        box.close()
