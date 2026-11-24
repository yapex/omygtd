"""严格入参的三个写命令（add / defer / clarify）。

口径（老板 2026-11-24 拍板）：换算（「明天」→ 2026-12-01）由调用方 LLM 做，
CLI 只吃标准格式 —— ISO 日期 + 24 小时钟点。所以这里断言两件事：

1. 四种时间形态（无时间 / 只日期 / 时段 / 钟点）传给 store 的参数**精确到分钟**；
2. 口语与非法值一律退出码 2，一条都不写（store 的调用记录为空）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from gtd import cli
from gtd.inbox import BoardIndex, Inbox
from gtd.reminders import TEST_LIST, FakeStore

NOW = "2026-11-23 09:00"      # 周一
DUE = "2026-12-01"            # 下周二
NEXT_DUE = "2026-12-08"


class RecordingStore(FakeStore):
    """记下 add 的每一个实参：断言「传给 store 的值精确到分钟」。"""

    def __init__(self, lists=(TEST_LIST,)):
        super().__init__(lists=lists)
        self.adds: list[dict] = []

    def add(self, title, due_at, list_name, **kwargs):
        self.adds.append({"title": title, "due_at": due_at, "list": list_name,
                          **kwargs})
        return super().add(title, due_at, list_name, **kwargs)


@pytest.fixture()
def fake():
    return RecordingStore()


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


def run(capsys, argv, *, db, store, now=NOW):
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


def stub_render():
    def fake_render(entries, *, now, out_path):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)
    return fake_render


def board(capsys, db, store, tmp_path, monkeypatch) -> dict:
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    return run(capsys, ["board", "--out", str(tmp_path / "b.png")],
               db=db, store=store)[1]


def seed(capsys, db, store, tmp_path, monkeypatch, argv=("--due", DUE)) -> dict:
    """先记一条带时间的任务，再出一张图（拿到图上号）。"""
    added = run(capsys, ["add", "交材料", *argv], db=db, store=store)[1]
    board(capsys, db, store, tmp_path, monkeypatch)
    return added


def item_of(store, reminder_id: str) -> dict:
    return next(item for item in store.all() if item["id"] == reminder_id)


# ---------------------------------------------------------------- add：四种形态

def test_add_date_only_is_all_day_without_alarm(capsys, db, fake):
    code, data = run(capsys, ["add", "交材料", "--due", DUE], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    sent = fake.adds[-1]
    assert sent["due_at"] == datetime(2026, 12, 1, 0, 0)
    assert sent["all_day"] is True and sent["slot"] is None
    assert sent["advance_minutes"] is None and sent["repeat"] is None
    assert data["due"] == DUE and data["at"] is None and data["all_day"] is True
    stored = fake.all()[0]
    assert stored["dueDate"] == DUE and stored["dueDateIsAllDay"] is True
    assert stored["alarmDate"] == ""


@pytest.mark.parametrize("slot,alarm", [("am", "2026-12-01 12:00"),
                                        ("pm", "2026-12-01 18:00")])
def test_add_slot_keeps_all_day_and_sets_the_slot_alarm(capsys, db, fake, slot, alarm):
    code, data = run(capsys, ["add", "买菜", "--due", DUE, "--slot", slot],
                     db=db, store=fake)
    assert code == 0 and data["slot"] == slot and data["all_day"] is True
    sent = fake.adds[-1]
    assert sent["due_at"] == datetime(2026, 12, 1, 0, 0)
    assert sent["all_day"] is True and sent["slot"] == slot
    assert item_of(fake, data["reminder_id"])["alarmDate"] == alarm


def test_add_clock_is_the_exact_moment(capsys, db, fake):
    code, data = run(capsys, ["add", "开会", "--due", DUE, "--at", "09:05"],
                     db=db, store=fake)
    assert code == 0 and data["at"] == "09:05" and data["all_day"] is False
    sent = fake.adds[-1]
    assert sent["due_at"] == datetime(2026, 12, 1, 9, 5)      # 精确到分钟
    assert sent["all_day"] is False and sent["slot"] is None
    assert sent["advance_minutes"] == 0                        # 到点响
    stored = item_of(fake, data["reminder_id"])
    assert stored["dueDate"] == "2026-12-01 09:05"
    assert stored["alarmDate"] == "2026-12-01 09:05"
    assert stored["dueDateIsAllDay"] is False


def test_add_without_due_lands_in_inbox_with_the_title_untouched(capsys, db, fake):
    """没给 --due 就是收件箱；标题原样入库，不含任何词剥离。"""
    code, data = run(capsys, ["add", "明天中午前交材料"], db=db, store=fake)
    assert code == 0 and data["source"] == "inbox" and data["due"] is None
    assert data["title"] == "明天中午前交材料"
    assert [row["title"] for row in Inbox(db).pending()] == ["明天中午前交材料"]
    assert fake.adds == []


def test_add_note_is_still_passed_through(capsys, db, fake):
    run(capsys, ["add", "交材料", "--due", DUE, "--note", "带原件"], db=db, store=fake)
    assert fake.adds[-1]["note"] == "带原件"


# ---------------------------------------------------------------- add：repeat

@pytest.mark.parametrize("frequency,interval,expected", [
    ("daily", 1, "daily"),
    ("weekly", 1, "weekly"),
    ("monthly", 1, "monthly"),
    ("daily", 3, "every 3 days"),
    ("weekly", 2, "every 2 weeks"),
    ("monthly", 2, "every 2 months"),
])
def test_add_repeat_maps_frequency_and_interval(capsys, db, fake,
                                                frequency, interval, expected):
    """``--due`` 那天就是锚点；频率 + 间隔映射成 Reminder 原生规则写法。"""
    code, data = run(capsys, ["add", "例会", "--due", DUE, "--repeat", frequency,
                             "--repeat-interval", str(interval)], db=db, store=fake)
    assert code == 0 and data["repeat"] == expected
    sent = fake.adds[-1]
    assert sent["repeat"] == expected
    assert sent["due_at"].date() == date(2026, 12, 1), "锚点必须是 --due 那天"
    assert fake.all()[0]["repeat"] == expected


def test_add_repeat_defaults_to_interval_one(capsys, db, fake):
    _, data = run(capsys, ["add", "例会", "--due", DUE, "--repeat", "weekly"],
                  db=db, store=fake)
    assert data["repeat"] == "weekly" and fake.adds[-1]["repeat"] == "weekly"


# ---------------------------------------------------------------- add：优先级

def test_add_priority_flag_sets_the_inbox_level(capsys, db, fake):
    code, data = run(capsys, ["add", "做分析", "--priority", "high"], db=db, store=fake)
    assert code == 0 and data["priority"] == "high"
    assert Inbox(db).pending()[0]["priority"] == "high"


def test_add_priority_flag_reaches_the_reminder(capsys, db, fake):
    _, data = run(capsys, ["add", "交材料", "--due", DUE, "--priority", "medium"],
                  db=db, store=fake)
    assert data["source"] == "reminder" and data["priority"] == "medium"
    assert fake.adds[-1]["priority"] == "medium"
    assert fake.all()[0]["priority"] == "medium"


def test_add_defaults_to_no_priority(capsys, db, fake):
    _, data = run(capsys, ["add", "交材料", "--due", DUE], db=db, store=fake)
    assert data["priority"] == "none" and fake.adds[-1]["priority"] == "none"


# ---------------------------------------------------------------- defer

@pytest.mark.parametrize("argv,expected_at,expected_all_day,expected_alarm", [
    (["--due", NEXT_DUE], datetime(2026, 12, 8, 0, 0), True, ""),
    (["--due", NEXT_DUE, "--slot", "am"], datetime(2026, 12, 8, 0, 0), True,
     "2026-12-08 12:00"),
    (["--due", NEXT_DUE, "--at", "18:45"], datetime(2026, 12, 8, 18, 45), False,
     "2026-12-08 18:45"),
])
def test_defer_passes_the_exact_moment_to_the_store(capsys, db, fake, tmp_path,
                                                    monkeypatch, argv, expected_at,
                                                    expected_all_day, expected_alarm):
    added = seed(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["defer", "1", *argv], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    sent = fake.rescheduled[-1]
    assert sent["id"] == added["reminder_id"]
    assert sent["due_at"] == expected_at          # 精确到分钟
    assert sent["all_day"] is expected_all_day
    assert sent["alarm"] == expected_alarm
    stored = item_of(fake, added["reminder_id"])
    assert stored["dueDate"] == expected_alarm or stored["dueDate"] == NEXT_DUE
    assert stored["alarmDate"] == expected_alarm


def test_defer_keeps_the_board_number(capsys, db, fake, tmp_path, monkeypatch):
    added = seed(capsys, db, fake, tmp_path, monkeypatch)
    run(capsys, ["defer", "1", "--due", NEXT_DUE], db=db, store=fake)
    board(capsys, db, fake, tmp_path, monkeypatch)
    ref = BoardIndex(db).lookup(1, day=date(2026, 11, 23))
    assert ref == f"reminder:{added['reminder_id']}"


def test_defer_updates_multiple_numbers(capsys, db, fake, tmp_path, monkeypatch):
    seed(capsys, db, fake, tmp_path, monkeypatch)
    run(capsys, ["add", "回邮件", "--due", "2026-11-25", "--slot", "pm"],
        db=db, store=fake)
    board(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["defer", "1", "2", "--due", NEXT_DUE], db=db, store=fake)
    assert code == 0 and [item["seq"] for item in data["results"]] == [1, 2]
    assert all(item["due"] == NEXT_DUE for item in data["results"])
    assert len(fake.rescheduled) == 2


def test_defer_rejects_a_repeat_item(capsys, db, fake, tmp_path, monkeypatch):
    run(capsys, ["add", "例会", "--due", "2026-11-30", "--repeat", "weekly"],
        db=db, store=fake)
    board(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["defer", "1", "--due", NEXT_DUE], db=db, store=fake)
    assert code == 1 and data["results"][0]["ok"] is False
    assert "重复任务暂不支持 defer" in data["results"][0]["error"]
    assert fake.rescheduled == []


def test_defer_on_an_inbox_number_points_at_clarify(capsys, db, fake, tmp_path,
                                                    monkeypatch):
    run(capsys, ["add", "买加湿器"], db=db, store=fake)
    board(capsys, db, fake, tmp_path, monkeypatch)
    code, data = run(capsys, ["defer", "1", "--due", NEXT_DUE], db=db, store=fake)
    assert code == 1
    assert f"gtd clarify 1 --due {NEXT_DUE}" in data["results"][0]["error"]
    assert fake.rescheduled == []


# ---------------------------------------------------------------- clarify

def test_clarify_turns_a_thought_into_an_all_day_reminder(capsys, db, fake):
    run(capsys, ["add", "回设计稿"], db=db, store=fake)
    row = Inbox(db).pending()[0]
    code, data = run(capsys, ["clarify", str(row["id"]), "--due", DUE],
                     db=db, store=fake)
    assert code == 0 and data["ok"] is True and data["title"] == "回设计稿"
    sent = fake.adds[-1]
    assert sent["title"] == "回设计稿" and sent["note"] == row["note"]
    assert sent["due_at"] == datetime(2026, 12, 1, 0, 0)
    assert sent["all_day"] is True and sent["slot"] is None
    assert Inbox(db).pending() == []


@pytest.mark.parametrize("argv,expected", [
    (("--due", DUE, "--slot", "pm"), datetime(2026, 12, 1, 0, 0)),
    (("--due", DUE, "--at", "07:15"), datetime(2026, 12, 1, 7, 15)),
])
def test_clarify_accepts_slot_and_clock(capsys, db, fake, argv, expected):
    run(capsys, ["add", "回设计稿"], db=db, store=fake)
    row = Inbox(db).pending()[0]
    code, _data = run(capsys, ["clarify", str(row["id"]), *argv], db=db, store=fake)
    assert code == 0 and fake.adds[-1]["due_at"] == expected


def test_clarify_carries_the_inbox_priority(capsys, db, fake):
    run(capsys, ["add", "做分析", "--priority", "high"], db=db, store=fake)
    row = Inbox(db).pending()[0]
    code, data = run(capsys, ["clarify", str(row["id"]), "--due", DUE],
                     db=db, store=fake)
    assert code == 0 and data["priority"] == "high"
    assert fake.adds[-1]["priority"] == "high"


# ---------------------------------------------------------------- 非法值 → 2

@pytest.mark.parametrize("argv,needle", [
    (["add", "交材料", "--due", "明天"], "YYYY-MM-DD"),
    (["add", "交材料", "--due", "2026-02-30"], "YYYY-MM-DD"),
    (["add", "交材料", "--due", DUE, "--at", "9点"], "HH:MM"),
    (["add", "交材料", "--due", DUE, "--at", "9:00"], "HH:MM"),
    (["add", "交材料", "--due", DUE, "--at", "09:00", "--slot", "am"], "互斥"),
    (["add", "交材料", "--at", "09:00"], "--due"),
    (["add", "交材料", "--slot", "pm"], "--due"),
    (["add", "例会", "--repeat", "weekly"], "--due"),
    (["add", "例会", "--due", DUE, "--repeat-interval", "2"], "--repeat-interval"),
    (["add", "例会", "--due", DUE, "--repeat", "daily", "--repeat-interval", "0"],
     "正整数"),
    (["defer", "1", "--due", "明天"], "YYYY-MM-DD"),
    (["defer", "1"], "--due"),
    (["clarify", "1"], "--due"),
    (["clarify", "1", "--due", "2026-12-01", "--at", "9点"], "HH:MM"),
])
def test_non_standard_values_are_parameter_errors(capsys, db, fake, argv, needle):
    """口语与非法值：退出码 2，文案给人看，store 一次都没被调用。"""
    code, data = run(capsys, argv, db=db, store=fake)
    assert code == 2 and data["ok"] is False
    assert needle in data["error"]
    assert fake.adds == [] and fake.rescheduled == [] and fake.all() == []
