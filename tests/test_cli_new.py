"""命令层 v2（cli.py 重写）的测试：全部 FakeStore + 临时库，零 Reminder 写入。

两个隔离手段：
1. ``store=FakeStore(...)`` 从 main() 注入 —— 注入时连 ``RemindctlAdapter`` 都不会构造
   （见 ``test_injected_store_skips_real_adapter``）。
2. 库用 ``tmp_path`` 下的临时文件（不是 ``:memory:``）：一个用例里要跨多次
   ``cli.main()`` 调用共享状态，而两条 ``:memory:`` 连接是两个互不相干的库。
   这是项目里既有的约定（见 skill gtd-project-ops：「命令层测试用 tmp_path 文件库」）。

``reminders._guard_list`` 在 pytest 里禁止 GTD/Waiting 这两个名字，FakeStore 也照拦；
测试把它放开——真实 adapter 用不到，所以拦不拦都不影响「零 Reminder 写入」。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, time
from pathlib import Path

import pytest

from gtd import cli, reminders
from gtd.inbox import Inbox, Mirror
from gtd.reminders import FakeStore

NOW = "2026-11-23 09:00"


# ---------------------------------------------------------------- 夹具与工具

@pytest.fixture()
def no_guard(monkeypatch):
    """放开 reminders 的列表守卫：FakeStore 不碰真实 Reminder，拦不拦都一样。"""
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def fake(no_guard):
    return FakeStore(lists=("GTD", "Waiting"))


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


class BrokenStore(FakeStore):
    """Reminder 写不进去的存储：用来验「念头不许丢」。"""

    def add(self, *args, **kwargs) -> str:
        return ""

    def complete(self, reminder_id: str) -> bool:
        return False


def run(capsys, argv, *, db, store, now=NOW):
    """跑一次 cli.main，返回 (退出码, JSON 载荷)。"""
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


def rows(db, sql, *params):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


# ---------------------------------------------------------------- add

def test_add_with_time_returns_seq_list_due_at(capsys, db, fake):
    code, data = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["seq"] == 1 and data["title"] == "交材料"
    assert data["list"] == "GTD" and data["due"] == "2026-11-24" and data["at"] == "12:00"
    assert data["matched"] == "明天中午前" and data["source"] == "reminder"
    assert data["reminder_id"].startswith("fake-")


def test_add_with_time_lands_in_store(capsys, db, fake):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    items = fake.all()
    assert [item["title"] for item in items] == ["交材料"]
    assert items[0]["listName"] == "GTD"
    assert items[0]["dueDate"] == "2026-11-24 12:00"


def test_add_without_time_goes_to_inbox(capsys, db, fake):
    code, data = run(capsys, ["add", "想给家里买个加湿器"], db=db, store=fake)
    assert code == 0 and data["source"] == "inbox"
    assert data["seq"] == 1 and data["due"] is None and data["at"] is None
    assert "reminder_id" not in data
    assert fake.all() == []
    assert [row["title"] for row in Inbox(db).pending()] == ["想给家里买个加湿器"]
    assert data["inbox_id"] == Inbox(db).pending()[0]["id"]


def test_add_reminder_failure_keeps_thought_in_inbox(capsys, db, no_guard):
    broken = BrokenStore(lists=("GTD", "Waiting"))
    code, data = run(capsys, ["add", "明天中午前交材料"], db=db, store=broken)
    assert code == 1 and data["ok"] is False
    assert "Reminder" in data["error"]
    rows_in_inbox = Inbox(db).pending()
    assert [row["title"] for row in rows_in_inbox] == ["交材料"]
    assert data["inbox_id"] == rows_in_inbox[0]["id"]
    assert data["due"] == "2026-11-24"          # 本来想要的时间照样报出来


def test_add_twice_within_window_is_idempotent(capsys, db, fake):
    _, first = run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    _, second = run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    assert first["inbox_id"] == second["inbox_id"]
    assert first["seq"] == second["seq"] == 1
    assert len(Inbox(db).pending()) == 1


def test_injected_store_skips_real_adapter(capsys, db, fake, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("注入 store 时不许构造真实 adapter")
    monkeypatch.setattr(cli, "RemindctlAdapter", boom)
    code, data = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    assert code == 0 and data["ok"] is True


def test_memory_db_single_call(capsys, fake):
    code, data = run(capsys, ["add", "想到一个点子"], db=":memory:", store=fake)
    assert code == 0 and data["source"] == "inbox" and data["seq"] == 1


# ---------------------------------------------------------------- inbox

def test_inbox_lists_pending_with_oldest_days(capsys, db, fake):
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    run(capsys, ["add", "问问老王那个事"], db=db, store=fake)
    code, data = run(capsys, ["inbox"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["count"] == 2 and data["oldest_days"] == 0
    assert [item["title"] for item in data["items"]] == ["想到一个点子", "问问老王那个事"]
    assert [item["seq"] for item in data["items"]] == [1, 2]   # 号跟 board 上印的一致
    assert all(item["age_days"] == 0 for item in data["items"])


# ---------------------------------------------------------------- clarify

def test_clarify_marks_inbox_row_moved(capsys, db, fake):
    run(capsys, ["add", "回设计稿"], db=db, store=fake)
    inbox_id = Inbox(db).pending()[0]["id"]
    code, data = run(capsys, ["clarify", str(inbox_id), "明天下午"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["title"] == "回设计稿"                  # 只说了时间，标题沿用 inbox 的
    # 下午 = 全天 + 18:00 响（t_0541f6c3）：at 不再报 18:00，改报 slot
    assert data["due"] == "2026-11-24" and data["at"] is None and data["slot"] == "pm"
    assert data["list"] == "GTD" and data["inbox_id"] == inbox_id
    assert Inbox(db).pending() == []
    moved = rows(db, "select moved_at, reminder_id from inbox where id=?", inbox_id)[0]
    assert moved["moved_at"] is not None
    assert moved["reminder_id"] == data["reminder_id"]
    assert fake.all()[0]["isCompleted"] is False


def test_clarify_without_time_fails_and_keeps_inbox(capsys, db, fake):
    run(capsys, ["add", "回设计稿"], db=db, store=fake)
    inbox_id = Inbox(db).pending()[0]["id"]
    code, data = run(capsys, ["clarify", str(inbox_id), "就是那个事"], db=db, store=fake)
    assert code == 1 and data["ok"] is False and data["error"] == "没听出时间"
    assert len(Inbox(db).pending()) == 1
    assert rows(db, "select moved_at from inbox where id=?", inbox_id)[0]["moved_at"] is None
    assert fake.all() == []


def test_clarify_reminder_failure_keeps_inbox_untouched(capsys, db, no_guard):
    broken = BrokenStore(lists=("GTD", "Waiting"))
    run(capsys, ["add", "回设计稿"], db=db, store=broken)
    inbox_id = Inbox(db).pending()[0]["id"]
    code, data = run(capsys, ["clarify", str(inbox_id), "明天下午"], db=db, store=broken)
    assert code == 1 and data["ok"] is False and data["inbox_id"] == inbox_id
    assert len(Inbox(db).pending()) == 1
    assert rows(db, "select moved_at from inbox where id=?", inbox_id)[0]["moved_at"] is None


def test_clarify_accepts_the_number_seen_on_the_board(capsys, db, fake):
    """老板看到的是当日序号：先占一个号给 Reminder，再记一条念头（id=1、号=2）。"""
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)     # 号 1
    _, parked = run(capsys, ["add", "回设计稿"], db=db, store=fake)  # 号 2，inbox id 1
    assert parked["seq"] == 2 and parked["inbox_id"] == 1
    code, data = run(capsys, ["clarify", "2", "明天下午"], db=db, store=fake)
    assert code == 0 and data["ok"] is True and data["inbox_id"] == 1
    assert [row["title"] for row in Inbox(db).pending()] == []


def test_clarify_rejects_a_number_taken_by_a_reminder(capsys, db, fake):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)      # 号 1 = reminder
    run(capsys, ["add", "回设计稿"], db=db, store=fake)              # 号 2 = inbox id 1
    code, data = run(capsys, ["clarify", "1", "明天下午"], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    assert len(Inbox(db).pending()) == 1            # 别错迁了别的念头


def test_clarify_falls_back_to_inbox_row_id(capsys, db, fake):
    row_id = Inbox(db).add("没有号的念头", now=datetime(2026, 11, 23, 8, 0))
    code, data = run(capsys, ["clarify", str(row_id), "明天下午"], db=db, store=fake)
    assert code == 0 and data["inbox_id"] == row_id
    assert Inbox(db).pending() == []


def test_clarify_unknown_id_reports_error(capsys, db, fake):
    code, data = run(capsys, ["clarify", "42", "明天下午"], db=db, store=fake)
    assert code == 1 and data["ok"] is False and "没有 #42" in data["error"]
    assert fake.all() == []


# ---------------------------------------------------------------- done
#
# 号全来自 board_index（今天的图上号）。所以每个用例都先出一张假图，
# 再按图上的号 `done`——跟老板的说法一致：「图上的 3 号」。

def fake_board(capsys, db, store, tmp_path, monkeypatch) -> dict:
    """出一张假图（替掉真 Chrome），返回载荷（含 numbers）。"""
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    return run(capsys, ["board", "--out", str(tmp_path / "b.png")],
               db=db, store=store)[1]


def test_done_completes_the_number_on_the_board(capsys, db, fake, tmp_path, monkeypatch):
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    assert fake_board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1]
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    result = data["results"][0]
    assert result["ok"] is True and result["ref"] == f"reminder:{added['reminder_id']}"
    assert fake.all()[0]["isCompleted"] is True


def test_done_completes_multiple_numbers(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    run(capsys, ["add", "后天下午回邮件"], db=db, store=fake)
    assert fake_board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1, 2]
    code, data = run(capsys, ["done", "1", "2"], db=db, store=fake)
    assert code == 0 and [item["seq"] for item in data["results"]] == [1, 2]
    assert all(item["ok"] for item in data["results"])
    assert all(item["isCompleted"] for item in fake.all())


def test_done_without_board_says_take_a_look_first(capsys, db, fake):
    """今天还没出过图 → 不许按 daily_seq 瞎猜，直接让人先看图。"""
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 1 and data["results"][0]["ok"] is False
    assert "先看一眼看板" in data["results"][0]["error"]
    assert not any(item["isCompleted"] for item in fake.all()), "一条都不许动"


def test_done_unknown_number_reports_error_without_crashing(capsys, db, fake,
                                                            tmp_path, monkeypatch):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["done", "99"], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    assert data["results"][0]["ok"] is False
    assert data["results"][0]["error"] == "今天没有 99 号"


def test_done_on_inbox_number_marks_moved(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    assert fake_board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1]
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 0 and data["results"][0]["source"] == "inbox"
    assert Inbox(db).pending() == []


def test_done_on_already_clarified_inbox_number_says_where_to_look(capsys, db, fake,
                                                                   tmp_path, monkeypatch):
    """出图后把那条念头 clarify 走了：图上号还指着它，要报清楚而不是静默失败。"""
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    row_id = Inbox(db).pending()[0]["id"]
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    run(capsys, ["clarify", "1", "明天下午"], db=db, store=fake)
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 1 and data["results"][0]["ok"] is False
    assert data["results"][0]["ref"] == f"inbox:{row_id}"
    assert "已经迁出收件箱" in data["results"][0]["error"]


def test_done_refuses_when_reminder_completed_on_phone(capsys, db, fake, tmp_path,
                                                       monkeypatch):
    """图上出完之后在手机上完成了这条：号还在，但不能再做一次。"""
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    fake.complete(fake.all()[0]["id"])
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 1 and "已经完成" in data["results"][0]["error"]


# ---------------------------------------------------------------- board

def stub_render():
    """替掉真 Chrome：写个假 PNG 就返回（断言只看载荷与镜像，不看像素）。"""
    def fake_render(entries, *, now, out_path):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)
    return fake_render


def mirror_ids(db):
    return sorted(row["reminder_id"] for row in rows(db, "select reminder_id from mirror"))


def test_board_counts_match_data(capsys, db, fake, tmp_path, monkeypatch):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("昨天该做完的", datetime(2026, 11, 22, 18, 0), "GTD")
    fake.add("等对方回话", datetime(2026, 11, 24, 18, 0), "Waiting")   # 明天，不进今日图
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)            # inbox 一条
    seen = {}

    def fake_render(entries, *, now, out_path):
        seen["entries"] = entries
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)

    monkeypatch.setattr(cli.render, "render_today", fake_render)
    out = tmp_path / "today.png"
    code, data = run(capsys, ["board", "--out", str(out)], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["count"] == 4 and data["overdue"] == 1 and data["inbox"] == 1
    assert data["path"] == str(out) and out.exists()
    assert len(seen["entries"]) == 4
    assert [entry.title for entry in seen["entries"]][0] == "今天交材料"
    assert seen["entries"][2].due.isoformat() == "2026-11-24"   # 明天进「接下来」区，带日期
    assert seen["entries"][-1].kind == "inbox"


def test_board_passes_seven_day_window_to_render(capsys, db, fake, tmp_path, monkeypatch):
    """窗口边界：7 天内（含第 7 天）的条目传给 render（进「接下来」区），第 8 天的不传。"""
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("第七天的事", datetime(2026, 11, 30, 12, 0), "GTD")
    fake.add("第八天的事", datetime(2026, 12, 1, 12, 0), "GTD")
    seen = {}

    def fake_render(entries, *, now, out_path):
        seen["entries"] = entries
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)

    monkeypatch.setattr(cli.render, "render_today", fake_render)
    code, data = run(capsys, ["board", "--out", str(tmp_path / "x.png")], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    titles = [entry.title for entry in seen["entries"]]
    assert titles == ["今天交材料", "第七天的事"], titles


# ---------------------------------------------------------------- 图上号（board_index）
#
# 硬要求：当天编号幂等。同样的数据反复出图，号一个字都不许变；新增追加到末尾；
# 删掉的号不回收、其余不重排。

def board_rows(db):
    """今天的映射（按号排）：[(号, 引用)]，用来断言「号没动」。"""
    return [(row["display_seq"], row["reminder_id"])
            for row in rows(db, "select display_seq, reminder_id from board_index "
                                "where day='2026-11-23' order by display_seq")]


def test_board_numbers_are_idempotent_across_five_runs(capsys, db, fake, out_dir,
                                                      monkeypatch):
    fake.add("甲", datetime(2026, 11, 23, 9, 0), "GTD")
    fake.add("乙", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("丙", datetime(2026, 11, 27, 15, 30), "GTD")
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    first = run(capsys, ["board"], db=db, store=fake)[1]["numbers"]
    snapshot = board_rows(db)
    later = [run(capsys, ["board"], db=db, store=fake)[1]["numbers"] for _ in range(4)]
    assert first == [1, 2, 3] and later == [[1, 2, 3]] * 4, (first, later)
    assert board_rows(db) == snapshot, "映射行一个字节都不许变"


def test_board_keeps_old_numbers_and_appends_new(capsys, db, fake, out_dir, monkeypatch):
    fake.add("甲", datetime(2026, 11, 23, 9, 0), "GTD")
    fake.add("乙", datetime(2026, 11, 23, 12, 0), "GTD")
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    assert run(capsys, ["board"], db=db, store=fake)[1]["numbers"] == [1, 2]
    fake.add("丙", datetime(2026, 11, 23, 8, 0), "GTD")     # 今早，图上排最前面
    data = run(capsys, ["board"], db=db, store=fake)[1]
    assert data["numbers"] == [3, 1, 2], "老号不动，新条目拿 max+1=3（哪怕它排最前）"


def test_board_drops_deleted_numbers_without_renumbering(capsys, db, fake, out_dir,
                                                         monkeypatch):
    fake.add("甲", datetime(2026, 11, 23, 9, 0), "GTD")
    gone = fake.add("乙", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("丙", datetime(2026, 11, 23, 15, 0), "GTD")
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    assert run(capsys, ["board"], db=db, store=fake)[1]["numbers"] == [1, 2, 3]
    fake.delete(gone)
    assert run(capsys, ["board"], db=db, store=fake)[1]["numbers"] == [1, 3], "2 号空着不填补"
    fake.add("丁", datetime(2026, 11, 23, 16, 0), "GTD")
    assert run(capsys, ["board"], db=db, store=fake)[1]["numbers"] == [1, 3, 4]


def test_board_leaves_daily_seq_alone(capsys, db, fake, out_dir, monkeypatch):
    """图上号与 daily_seq 是两套：出图不许改 daily_seq 一行。"""
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    before = rows(db, "select day, seq, ref from daily_seq order by seq")
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    run(capsys, ["board"], db=db, store=fake)
    run(capsys, ["board"], db=db, store=fake)
    assert rows(db, "select day, seq, ref from daily_seq order by seq") == before


def test_board_numbers_only_what_is_drawn(capsys, db, fake, out_dir, monkeypatch):
    """接下来区只露 5 条：第 6、7 条不上图，也就不占号（也不许崩）。"""
    for day in range(1, 8):
        fake.add(f"第{day}天的事", datetime(2026, 11, 23 + day, 9, 0), "GTD")
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    code, data = run(capsys, ["board"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["numbers"] == [1, 2, 3, 4, 5] and data["count"] == 5
    assert len(board_rows(db)) == 5


# ---------------------------------------------------------------- board → mirror

def test_board_rebuilds_mirror_with_all_open_reminders(capsys, db, fake, tmp_path, monkeypatch):
    """镜像存 Reminder 全量（含明天以后），不是「今天该看」那一份。"""
    today = fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    later = fake.add("等对方回话", datetime(2026, 11, 24, 18, 0), "Waiting")   # 不进今日图
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    out = tmp_path / "today.png"
    code, data = run(capsys, ["board", "--out", str(out)], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert "warnings" not in data
    assert data["count"] == 3 and data["inbox"] == 1   # 今天那条 + 明天那条（接下来区）+ 1 条 inbox
    assert mirror_ids(db) == sorted([today, later])
    assert len(mirror_ids(db)) == len(fake.all()) == 2


def test_board_survives_mirror_failure(capsys, db, fake, tmp_path, monkeypatch):
    """镜像只是备份：写失败要进 warnings，board 仍然成功、退出码 0。"""
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")

    class BadMirror:
        def __init__(self, *_args, **_kwargs):
            pass

        def replace_all(self, *_args, **_kwargs):
            raise RuntimeError("disk is on fire")

    monkeypatch.setattr(cli, "Mirror", BadMirror)
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    out = tmp_path / "today.png"
    code, data = run(capsys, ["board", "--out", str(out)], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["warnings"] and "disk is on fire" in data["warnings"][0]
    assert out.exists() and mirror_ids(db) == []


def test_board_render_failure_does_not_write_mirror(capsys, db, fake, tmp_path, monkeypatch):
    """出图失败就不写镜像：否则会记下一个老板根本没看到的视图。"""
    Mirror(db).replace_all([{"reminder_id": "stale-0001", "title": "上次出图的残留",
                             "list_name": "GTD", "due_at": None, "completed": 0}])
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")

    def boom(entries, *, now, out_path):
        raise RuntimeError("chrome 没了")

    monkeypatch.setattr(cli.render, "render_today", boom)
    code, data = run(capsys, ["board", "--out", str(tmp_path / "today.png")], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    assert mirror_ids(db) == ["stale-0001"]         # 原样不动


# ---------------------------------------------------------------- board → 缓存绕开
#
# 事故背景：同一天出两次图都写到 out/today-YYYYMMDD.png，推给老板后他手机上看
# 到的还是第一版——客户端按路径缓存，文件名没变就不重新取。所以现在默认落两个
# 文件：唯一名（推送用）＋ 当天最新副本（cron 引用）。

def counting_render():
    """每次出图写不同字节，用来区分「这一版」和「上一版」。"""
    state = {"n": 0}

    def fake_render(entries, *, now, out_path):
        state["n"] += 1
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([state["n"]]))
        return Path(out_path)

    return fake_render


@pytest.fixture()
def out_dir(tmp_path, monkeypatch):
    """把 GTD_OUT 指到 tmp：默认落盘别写进项目 out/。"""
    folder = tmp_path / "out"
    monkeypatch.setenv("GTD_OUT", str(folder))
    return folder


def test_board_without_out_writes_unique_and_latest(capsys, db, fake, out_dir,
                                                    monkeypatch):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    code, data = run(capsys, ["board"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["path"] != data["latest"]
    assert Path(data["path"]).exists() and Path(data["latest"]).exists()
    assert Path(data["latest"]).name == "today-20261123.png"
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(
        [Path(data["path"]).name, "today-20261123.png"])


def test_board_with_explicit_out_writes_only_that_file(capsys, db, fake, out_dir,
                                                       tmp_path, monkeypatch):
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    explicit = tmp_path / "explicit.png"
    code, data = run(capsys, ["board", "--out", str(explicit)], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["path"] == str(explicit) and Path(explicit).exists()
    assert data["latest"] in (None, data["path"])
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


def test_board_twice_latest_matches_second_unique(capsys, db, fake, out_dir,
                                                  monkeypatch):
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    first = run(capsys, ["board"], db=db, store=fake)[1]
    second = run(capsys, ["board"], db=db, store=fake)[1]
    assert first["path"] != second["path"]          # 同一秒内撞名会自动顺延
    latest = Path(second["latest"])
    assert latest.read_bytes() == Path(second["path"]).read_bytes()
    assert latest.read_bytes() != Path(first["path"]).read_bytes()
    assert latest.read_bytes() == b"\x89PNG\r\n\x1a\n\x02"   # 第二版的内容


def test_board_unique_name_matches_stamp_pattern(capsys, db, fake, out_dir,
                                                 monkeypatch):
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    data = run(capsys, ["board"], db=db, store=fake)[1]
    name = Path(data["path"]).name
    assert re.fullmatch(r"board-\d{8}-\d{6}\.png", name), name


def test_board_creates_missing_out_dir(capsys, db, fake, tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "out"
    monkeypatch.setenv("GTD_OUT", str(nested))
    monkeypatch.setattr(cli.render, "render_today", counting_render())
    code, data = run(capsys, ["board"], db=db, store=fake)
    assert code == 0 and Path(data["path"]).exists() and Path(data["latest"]).exists()


# ---------------------------------------------------------------- 注入与退出码

def test_now_injection_works_across_midnight(capsys, db, fake):
    _, late = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake,
                  now="2026-11-23 23:59")
    assert late["due"] == "2026-11-24" and late["seq"] == 1
    _, early = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake,
                   now="2026-11-24 00:01")
    assert early["due"] == "2026-11-25" and early["seq"] == 1


def test_unknown_command_exits_2(capsys, db, fake):
    code = cli.main(["--db", str(db), "wat"], store=fake)
    capsys.readouterr()
    assert code == 2


def test_missing_argument_exits_2(capsys, db, fake):
    code = cli.main(["--db", str(db), "add"], store=fake)
    capsys.readouterr()
    assert code == 2


def test_human_output_is_not_json(capsys, db, fake):
    code = cli.main(["--db", str(db), "--now", NOW, "--human", "add", "明天中午前交材料"],
                    store=fake)
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "✓ #1 交材料 · 2026-11-24 12:00"


def test_human_output_names_all_day_and_slots(capsys, db, fake):
    """--human 回执用「全天 / 上午 / 下午」同一个词，不印 00:00。"""
    for text, expected in (("明天 买菜", "· 2026-11-24 全天"),
                           ("明天上午买菜", "· 2026-11-24 上午"),
                           ("明天下午买菜", "· 2026-11-24 下午")):
        cli.main(["--db", str(db), "--now", NOW, "--human", "add", text], store=fake)
        assert capsys.readouterr().out.strip().endswith(expected), text


def test_list_counts_per_list(capsys, db, fake):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("买电池", datetime(2026, 11, 23, 20, 0), "GTD")
    fake.add("等对方回话", datetime(2026, 11, 24, 18, 0), "Waiting")
    code, data = run(capsys, ["list"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["lists"] == [{"name": "GTD", "total": 2, "open": 2},
                             {"name": "Waiting", "total": 1, "open": 1}]


# ---------------------------------------------------------------- 全天 / 上午 / 下午
# （t_0541f6c3：Reminder 的 due 与 alarm 独立 —— due 给全天，alarm 给精确时刻。）

def test_add_date_only_lands_as_all_day(capsys, db, fake):
    """只给日期 → Reminder 全天（due 不带时刻、无闹钟）。"""
    code, data = run(capsys, ["add", "明天 买菜"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["due"] == "2026-11-24" and data["at"] is None
    assert data["all_day"] is True and data["slot"] is None
    (item,) = fake.all()
    assert item["dueDate"] == "2026-11-24" and item["dueDateIsAllDay"] is True
    assert item["alarmDate"] == ""


def test_add_morning_slot_is_all_day_plus_noon_alarm(capsys, db, fake):
    code, data = run(capsys, ["add", "明天上午买菜"], db=db, store=fake)
    assert data["at"] is None and data["all_day"] is False and data["slot"] == "am"
    (item,) = fake.all()
    assert item["dueDate"] == "2026-11-24" and item["dueDateIsAllDay"] is True
    assert item["alarmDate"] == "2026-11-24 12:00"


def test_add_afternoon_slot_is_all_day_plus_evening_alarm(capsys, db, fake):
    code, data = run(capsys, ["add", "后天下午开会"], db=db, store=fake)
    assert data["slot"] == "pm" and data["due"] == "2026-11-25"
    (item,) = fake.all()
    assert item["dueDate"] == "2026-11-25" and item["dueDateIsAllDay"] is True
    assert item["alarmDate"] == "2026-11-25 18:00"


def test_add_precise_time_rings_at_that_moment(capsys, db, fake):
    """今晚 22:30 → due 与 alarm 都是 22:30（到点响一次，不再是「没闹钟」）。"""
    code, data = run(capsys, ["add", "今晚 22:30 卖出"], db=db, store=fake)
    assert data["at"] == "22:30" and data["all_day"] is False and data["slot"] is None
    (item,) = fake.all()
    assert item["dueDate"] == "2026-11-23 22:30"
    assert item["alarmDate"] == "2026-11-23 22:30"
    assert item["dueDateIsAllDay"] is False


def test_board_does_not_turn_all_day_midnight_into_a_clock(capsys, db, fake,
                                                           tmp_path, monkeypatch):
    """读路径：全天条目的 at 必须是 None（不许把本地 00:00 当时刻），闹钟另放。"""
    fake.add("买菜", datetime(2026, 11, 23), "GTD", all_day=True)
    fake.add("取快递", datetime(2026, 11, 23), "GTD", slot="pm")
    seen = {}

    def fake_render(entries, *, now, out_path):
        seen["entries"] = entries
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)

    monkeypatch.setattr(cli.render, "render_today", fake_render)
    code, _data = run(capsys, ["board", "--out", str(tmp_path / "x.png")],
                      db=db, store=fake)
    assert code == 0
    by_title = {entry.title: entry for entry in seen["entries"]}
    assert by_title["买菜"].at is None and by_title["买菜"].all_day is True
    assert by_title["买菜"].alarm is None
    assert by_title["取快递"].at is None and by_title["取快递"].alarm == time(18, 0)


# ---------------------------------------------------------------- view（文字视图）
#
# 老板要「改完 GTD 回 Markdown 文字，别再出图」（出图要 Chrome，2–3 秒）。
# 硬要求：view 与 board 的号必须一致（两边共用 _today_view + board_index 当天幂等），
# 且 view 也要写 board_index，否则「看了文字说 2 号完成」没法定位。

def run_text(capsys, argv, *, db, store, now=NOW):
    """跑一次 cli.main，返回 (退出码, 原始 stdout)——view 输出是 Markdown，不是 JSON。"""
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, capsys.readouterr().out


def md_numbers(text):
    """Markdown 表格里的号（数据行第一列）。"""
    return [int(match) for match in re.findall(r"^\| (\d+) \|", text, flags=re.M)]


def test_view_numbers_match_board_numbers(capsys, db, fake, tmp_path, monkeypatch):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("昨天该做完的", datetime(2026, 11, 22, 18, 0), "GTD")
    fake.add("把小米 SU7 送 4S 店保养", datetime(2026, 11, 27, 16, 0), "GTD")
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)          # 收件箱一条
    code, text = run_text(capsys, ["view"], db=db, store=fake)
    assert code == 0 and "**今天" in text
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    board = run(capsys, ["board", "--out", str(tmp_path / "b.png")], db=db, store=fake)[1]
    assert md_numbers(text) == board["numbers"] == [1, 2, 3, 4]


def test_view_writes_board_index_so_done_still_works(capsys, db, fake):
    """只看了 Markdown（没出图）也能按号完成——view 成功后就写了 board_index。"""
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    code, text = run_text(capsys, ["view"], db=db, store=fake)
    assert code == 0 and md_numbers(text) == [1]
    assert board_rows(db) == [(1, f"reminder:{added['reminder_id']}")]
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 0 and data["results"][0]["ok"] is True
    assert fake.all()[0]["isCompleted"] is True


def test_view_json_format_returns_structured_data(capsys, db, fake):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    code, text = run_text(capsys, ["view", "--format", "json"], db=db, store=fake)
    data = json.loads(text)                      # 结构化，不是 Markdown
    # 「今天」= 今日区行数：收件箱行不算在「今天」里（它自己有一格），所以是 1 件
    assert code == 0 and data["summary"] == {"today": 1, "overdue": 0, "inbox": 1}
    assert [item["number"] for item in data["today"]] == [1]
    assert data["today"][0]["when"] == "12:00"
    assert [item["title"] for item in data["inbox"]] == ["想到一个点子"]


def test_view_empty_store_renders_summary_only(capsys, db, fake):
    code, text = run_text(capsys, ["view"], db=db, store=fake)
    assert code == 0 and text.strip() == "**今天 0 件 · 逾期 0 件 · 收件箱 0 条**"
    assert board_rows(db) == []


def test_view_markdown_shows_overdue_tag_and_next_section(capsys, db, fake):
    fake.add("昨天该做完的", datetime(2026, 11, 22, 18, 0), "GTD")
    fake.add("买菜", datetime(2026, 11, 24), "GTD", all_day=True)
    _, text = run_text(capsys, ["view"], db=db, store=fake)
    assert "（逾期 1 天）" in text and "**接下来**" in text
    assert "周二 全天" in text


def test_human_list_prints_text_not_json(capsys, db, fake):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("买电池", datetime(2026, 11, 23, 20, 0), "GTD")
    fake.add("等对方回话", datetime(2026, 11, 24, 18, 0), "Waiting")
    code, text = run_text(capsys, ["--human", "list"], db=db, store=fake)
    assert code == 0 and not text.lstrip().startswith("{")
    assert "GTD" in text and "2 条（2 未完成）" in text
    assert "Waiting" in text and "1 条（1 未完成）" in text


# ---------------------------------------------------------------- priority
#
# 老板要的是「和 Reminder 里一致的重要级」：命令词汇照抄 remindctl
# （none|low|medium|high），只改收件箱条目——今天区/接下来区按日期排，不受影响。

def test_add_recognises_importance_and_strips_the_word(capsys, db, fake):
    code, data = run(capsys, ["add", "对腾讯Q3财报做综合分析，这个很重要"], db=db, store=fake)
    assert code == 0 and data["priority"] == "high"
    assert data["title"] == "对腾讯Q3财报做综合分析" and "重要" not in data["title"]
    assert Inbox(db).pending()[0]["priority"] == "high"


def test_add_priority_flag_overrides_parser(capsys, db, fake):
    _, data = run(capsys, ["add", "买鸡蛋", "--priority", "low"], db=db, store=fake)
    assert data["priority"] == "low"
    assert Inbox(db).pending()[0]["priority"] == "low"


def test_add_timed_task_writes_priority_into_reminder(capsys, db, fake):
    _, data = run(capsys, ["add", "明天中午前交材料，很重要"], db=db, store=fake)
    assert data["source"] == "reminder" and data["priority"] == "high"
    assert fake.all()[0]["priority"] == "high"


def test_priority_command_marks_inbox_row_and_sorts_it_first(capsys, db, fake,
                                                             tmp_path, monkeypatch):
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    run(capsys, ["add", "想给家里买个加湿器"], db=db, store=fake)
    assert fake_board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1, 2]
    code, data = run(capsys, ["priority", "2", "high"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["priority"] == "high" and data["mark"] == "!!!"
    assert Inbox(db).pending()[1]["priority"] == "high"
    _, text = run_text(capsys, ["view"], db=db, store=fake)
    assert text.index("| 2 | !!! 想给家里买个加湿器 |") < text.index("| 1 | 买鸡蛋 |")


def test_priority_none_clears_the_mark(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    run(capsys, ["priority", "1", "medium"], db=db, store=fake)
    _, marked = run_text(capsys, ["view"], db=db, store=fake)
    assert "| 1 | !! 买鸡蛋 |" in marked
    code, data = run(capsys, ["priority", "1", "none"], db=db, store=fake)
    assert code == 0 and data["mark"] == ""
    _, cleared = run_text(capsys, ["view"], db=db, store=fake)
    assert "| 1 | 买鸡蛋 |" in cleared and "!" not in cleared


def test_priority_rejects_level_outside_the_four(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["priority", "1", "garbage"], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    assert "none/low/medium/high" in data["error"]


def test_priority_rejects_a_reminder_number(capsys, db, fake, tmp_path, monkeypatch):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    assert fake_board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1]
    code, data = run(capsys, ["priority", "1", "high"], db=db, store=fake)
    assert code == 1 and "只有收件箱条目能设优先级" in data["error"]


def test_priority_rejects_unknown_number(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["priority", "99", "high"], db=db, store=fake)
    assert code == 1 and "今天没有 99 号" in data["error"]


def test_priority_keeps_board_numbers_stable_across_views(capsys, db, fake,
                                                          tmp_path, monkeypatch):
    """排序会变、号不许变：两边的号都来自 board_index 的当天映射。"""
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    run(capsys, ["add", "想给家里买个加湿器"], db=db, store=fake)
    fake_board(capsys, db, fake, tmp_path, monkeypatch)
    run(capsys, ["priority", "2", "high"], db=db, store=fake)
    _, first = run_text(capsys, ["view"], db=db, store=fake)
    _, second = run_text(capsys, ["view"], db=db, store=fake)
    assert md_numbers(first) == md_numbers(second) == [2, 1]
    assert board_rows(db) == [(1, "inbox:1"), (2, "inbox:2")]


def test_clarify_carries_inbox_priority_into_reminder(capsys, db, fake):
    run(capsys, ["add", "对腾讯Q3财报做综合分析，这个很重要"], db=db, store=fake)
    row_id = Inbox(db).pending()[0]["id"]
    code, data = run(capsys, ["clarify", str(row_id), "明天下午"], db=db, store=fake)
    assert code == 0 and data["priority"] == "high"
    assert fake.all()[0]["priority"] == "high", "clarify 之后 Reminder 里也要带 high"

