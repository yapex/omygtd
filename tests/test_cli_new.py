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
from datetime import datetime
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
    assert data["due"] == "2026-11-24" and data["at"] == "18:00"
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

def test_done_completes_reminder_by_seq(capsys, db, fake):
    _, added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    code, data = run(capsys, ["done", str(added["seq"])], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    result = data["results"][0]
    assert result["ok"] is True and result["ref"] == f"reminder:{added['reminder_id']}"
    assert fake.all()[0]["isCompleted"] is True


def test_done_completes_multiple_seqs(capsys, db, fake):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    run(capsys, ["add", "后天下午回邮件"], db=db, store=fake)
    code, data = run(capsys, ["done", "1", "2"], db=db, store=fake)
    assert code == 0 and [item["seq"] for item in data["results"]] == [1, 2]
    assert all(item["ok"] for item in data["results"])
    assert all(item["isCompleted"] for item in fake.all())


def test_done_unknown_seq_reports_error_without_crashing(capsys, db, fake):
    code, data = run(capsys, ["done", "99"], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    assert data["results"][0]["ok"] is False and "没有" in data["results"][0]["error"]


def test_done_on_inbox_seq_marks_moved(capsys, db, fake):
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 0 and data["results"][0]["source"] == "inbox"
    assert Inbox(db).pending() == []


def test_done_on_already_clarified_seq_says_where_to_look(capsys, db, fake):
    """clarify 之后旧号还指着那条已迁出的念头：报清楚，别静默失败。"""
    run(capsys, ["add", "想到一个点子"], db=db, store=fake)
    row_id = Inbox(db).pending()[0]["id"]
    run(capsys, ["clarify", "1", "明天下午"], db=db, store=fake)
    code, data = run(capsys, ["done", "1"], db=db, store=fake)
    assert code == 1 and data["results"][0]["ok"] is False
    assert data["results"][0]["ref"] == f"inbox:{row_id}"
    assert "已经迁出" in data["results"][0]["error"]


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
    assert data["count"] == 3 and data["overdue"] == 1 and data["inbox"] == 1
    assert data["path"] == str(out) and out.exists()
    assert len(seen["entries"]) == 3
    assert [entry.title for entry in seen["entries"]][0] == "今天交材料"
    assert seen["entries"][-1].kind == "inbox"


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
    assert data["count"] == 2 and data["inbox"] == 1   # 今天那条 + 1 条 inbox
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


def test_list_counts_per_list(capsys, db, fake):
    fake.add("今天交材料", datetime(2026, 11, 23, 12, 0), "GTD")
    fake.add("买电池", datetime(2026, 11, 23, 20, 0), "GTD")
    fake.add("等对方回话", datetime(2026, 11, 24, 18, 0), "Waiting")
    code, data = run(capsys, ["list"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["lists"] == [{"name": "GTD", "total": 2, "open": 2},
                             {"name": "Waiting", "total": 1, "open": 1}]
