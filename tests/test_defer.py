"""``gtd defer``：按图上号改到期时间（改期，不是完成）。

判据全部落在 **store 实际收到的参数** 上（``FakeStore.rescheduled`` 记的是传给
``reschedule`` 的 ``due_at`` 原值，精确到分钟），不只断言 ``ok=True`` ——
「改到哪一天几点」说错了，界面上照样是绿的。

时间语义与 ``add`` 同一套（都走 ``gtd/parser.py``）：
- 只给日期 → due 全天、无闹钟
- 上午 / 下午 → due 全天 + 12:00 / 18:00 闹钟
- 精确钟点 → due 就是那一刻
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from gtd import cli
from gtd.inbox import BoardIndex
from gtd.reminders import TEST_LIST, FakeStore, RemindctlAdapter

NOW = "2026-11-23 09:00"          # 周一
TOMORROW = "2026-11-24"
DAY_AFTER = "2026-11-25"


# ---------------------------------------------------------------- 夹具与工具

@pytest.fixture()
def fake():
    return FakeStore(lists=(TEST_LIST,))


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


def run(capsys, argv, *, db, store, now=NOW):
    """跑一次 cli.main，返回 (退出码, JSON 载荷)。"""
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


def stub_render():
    """替掉真 Chrome：写个假 PNG 就返回（只看载荷与编号，不看像素）。"""
    def fake_render(entries, *, now, out_path):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)
    return fake_render


def board(capsys, db, store, tmp_path, monkeypatch) -> dict:
    """出一张假图，拿到当天的图上号。"""
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    return run(capsys, ["board", "--out", str(tmp_path / "b.png")],
               db=db, store=store)[1]


def item_of(store, reminder_id: str) -> dict:
    return next(item for item in store.all() if item["id"] == reminder_id)


# ---------------------------------------------------------------- 只给日期

def test_defer_date_only_sets_all_day_without_alarm(capsys, db, fake,
                                                    tmp_path, monkeypatch):
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    board(capsys, db, fake, tmp_path, monkeypatch)

    code, data = run(capsys, ["defer", "1", "后天"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    result = data["results"][0]
    assert result["seq"] == 1 and result["ok"] is True
    assert result["due"] == DAY_AFTER and result["at"] is None
    assert result["all_day"] is True and result["slot"] is None
    assert result["source"] == "reminder"

    sent = fake.rescheduled[-1]              # 真正传给 store 的参数
    assert sent["id"] == added["reminder_id"]
    assert sent["due_at"] == datetime(2026, 11, 25, 0, 0)
    assert sent["all_day"] is True and sent["slot"] is None
    assert sent["due"] == DAY_AFTER and sent["alarm"] == ""

    stored = item_of(fake, added["reminder_id"])
    assert stored["dueDate"] == DAY_AFTER
    assert stored["dueDateIsAllDay"] is True
    assert stored["alarmDate"] == ""


# ---------------------------------------------------------------- 上午 / 下午

@pytest.mark.parametrize("when,slot,alarm", [
    ("明天上午", "am", "2026-11-24 12:00"),
    ("明天下午", "pm", "2026-11-24 18:00"),
])
def test_defer_slot_keeps_all_day_and_sets_slot_alarm(capsys, db, fake,
                                                      tmp_path, monkeypatch,
                                                      when, slot, alarm):
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    board(capsys, db, fake, tmp_path, monkeypatch)

    code, data = run(capsys, ["defer", "1", when], db=db, store=fake)
    assert code == 0
    result = data["results"][0]
    assert result["due"] == TOMORROW and result["slot"] == slot
    assert result["all_day"] is True and result["at"] is None

    sent = fake.rescheduled[-1]
    assert sent["due_at"] == datetime(2026, 11, 24, 0, 0)
    assert sent["due"] == TOMORROW and sent["alarm"] == alarm
    assert sent["all_day"] is True

    stored = item_of(fake, added["reminder_id"])
    assert stored["dueDate"] == TOMORROW
    assert stored["dueDateIsAllDay"] is True
    assert stored["alarmDate"] == alarm


# ---------------------------------------------------------------- 精确钟点

def test_defer_precise_clock_sets_exact_due(capsys, db, fake, tmp_path, monkeypatch):
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    board(capsys, db, fake, tmp_path, monkeypatch)

    code, data = run(capsys, ["defer", "1", "今晚10点半"], db=db, store=fake)
    assert code == 0
    result = data["results"][0]
    assert result["due"] == "2026-11-23" and result["at"] == "22:30"
    assert result["all_day"] is False and result["slot"] is None

    sent = fake.rescheduled[-1]
    assert sent["due_at"] == datetime(2026, 11, 23, 22, 30)   # 精确到分钟
    assert sent["due"] == "2026-11-23 22:30"
    assert sent["alarm"] == "2026-11-23 22:30"                # 与 add 同口径：到点响
    assert sent["all_day"] is False

    stored = item_of(fake, added["reminder_id"])
    assert stored["dueDate"] == "2026-11-23 22:30"
    assert stored["dueDateIsAllDay"] is False


# ---------------------------------------------------------------- 拒绝路径

def test_defer_unknown_number_is_rejected(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    board(capsys, db, fake, tmp_path, monkeypatch)

    code, data = run(capsys, ["defer", "99", "明天"], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    assert data["results"][0]["ok"] is False
    assert data["results"][0]["error"] == "今天没有 99 号"
    assert fake.rescheduled == [], "号不对一条都不许改"


def test_defer_without_board_asks_to_look_first(capsys, db, fake):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)

    code, data = run(capsys, ["defer", "1", "明天"], db=db, store=fake)
    assert code == 1
    assert "先看一眼看板" in data["results"][0]["error"]
    assert fake.rescheduled == []


def test_defer_completed_item_is_rejected(capsys, db, fake, tmp_path, monkeypatch):
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    board(capsys, db, fake, tmp_path, monkeypatch)
    fake.complete(added["reminder_id"])

    code, data = run(capsys, ["defer", "1", "明天"], db=db, store=fake)
    assert code == 1 and "已经完成" in data["results"][0]["error"]
    assert fake.rescheduled == []


def test_defer_inbox_thought_points_at_clarify(capsys, db, fake,
                                               tmp_path, monkeypatch):
    run(capsys, ["add", "想给家里买个加湿器"], db=db, store=fake)
    board(capsys, db, fake, tmp_path, monkeypatch)

    code, data = run(capsys, ["defer", "1", "明天"], db=db, store=fake)
    assert code == 1
    assert "clarify" in data["results"][0]["error"]
    assert fake.rescheduled == []


def test_defer_without_time_word_is_a_parameter_error(capsys, db, fake):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)

    code, data = run(capsys, ["defer", "1", "交材料"], db=db, store=fake)
    assert code == 2 and data["ok"] is False
    assert "没从" in data["error"] and fake.rescheduled == []


# ---------------------------------------------------------------- 多号 / 编号

def test_defer_updates_multiple_numbers_in_one_call(capsys, db, fake,
                                                    tmp_path, monkeypatch):
    run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    run(capsys, ["add", "后天下午回邮件"], db=db, store=fake)
    assert board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1, 2]

    code, data = run(capsys, ["defer", "1", "2", "明天"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert [item["seq"] for item in data["results"]] == [1, 2]
    assert all(item["ok"] and item["due"] == TOMORROW for item in data["results"])
    assert len(fake.rescheduled) == 2
    assert {item["dueDate"] for item in fake.all()} == {TOMORROW}


def test_defer_keeps_the_board_number(capsys, db, fake, tmp_path, monkeypatch):
    added = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)[1]
    board(capsys, db, fake, tmp_path, monkeypatch)

    run(capsys, ["defer", "1", "后天"], db=db, store=fake)
    assert board(capsys, db, fake, tmp_path, monkeypatch)["numbers"] == [1]
    ref = BoardIndex(db).lookup(1, day=date(2026, 11, 23))
    assert ref == f"reminder:{added['reminder_id']}"


# ---------------------------------------------------------------- adapter

def test_adapter_sends_due_and_clears_stale_alarm(monkeypatch):
    """全天改期要显式 ``--clear-alarm``：实测旧闹钟不会自己消失。"""
    adapter = RemindctlAdapter()
    calls = []

    class Proc:
        returncode = 0
        stdout = '{"id": "R1"}'

    monkeypatch.setattr(adapter, "_run", lambda args: calls.append(args) or Proc())
    assert adapter.reschedule("R1", datetime(2026, 11, 25), all_day=True) is True
    assert calls[-1] == ["edit", "R1", "--due", "2026-11-25", "--clear-alarm", "--json"]

    assert adapter.reschedule("R1", datetime(2026, 11, 24), all_day=True, slot="am")
    assert calls[-1] == ["edit", "R1", "--due", "2026-11-24",
                         "--alarm", "2026-11-24 12:00", "--json"]
