"""`scripts/migrate_reminders.py`（一次性迁移脚本）的测试。

零 Reminder 写入：全部走注入的假 runner（`main(run=...)`），连 remindctl 都不起。

口径：脚本是「旧系统 → 新系统」的一次性合并工具，不是 `gtd move` 子命令
（老板 2026-11-23：「这个是一次性的动作」）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_reminders.py"


def _load():
    spec = importlib.util.spec_from_file_location("migrate_reminders", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate = _load()


def reminder(rid, title, *, list_name="提醒事项", due="2027-03-01T12:00:00Z",
             all_day=False, alarm=None, priority="none", rule=None, done=False):
    data = {"id": rid, "title": title, "listName": list_name, "isCompleted": done,
            "dueDate": due, "dueDateIsAllDay": all_day, "priority": priority}
    if alarm:
        data["alarmDate"] = alarm
    if rule:
        data["recurrenceRule"] = rule
    return data


YEARLY = {"frequency": "yearly", "interval": 1}


class FakeRemindctl:
    """remindctl 的假实现：show / list / edit 三条命令够本脚本用。"""

    def __init__(self, items, *, lists=("提醒事项", "GTD"), fail_ids=()):
        self.items = [dict(item) for item in items]
        self.lists = list(lists)
        self.fail_ids = set(fail_ids)
        self.calls = []

    def _counts(self):
        counts = {name: 0 for name in self.lists}
        for item in self.items:
            if not item["isCompleted"]:
                counts[item["listName"]] = counts.get(item["listName"], 0) + 1
        return counts

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "show" and "--list" in args:
            name = args[args.index("--list") + 1]
            if name not in self.lists:
                return 1, "", f"List not found: {name}"
            rows = [item for item in self.items if item["listName"] == name]
            return 0, json.dumps(rows), ""
        if args[0] == "list":
            counts = self._counts()
            return 0, json.dumps([{"title": name, "reminderCount": count}
                                  for name, count in counts.items()]), ""
        if args[0] == "edit":
            if args[1] in self.fail_ids:
                return 1, "", "boom: edit failed"
            target = args[args.index("--list") + 1]
            for item in self.items:
                if item["id"] == args[1]:
                    item["listName"] = target
            return 0, "{}", ""
        return 2, "", f"unexpected args: {args}"

    @property
    def edits(self):
        return [call for call in self.calls if call[0] == "edit"]


def run_main(argv, fake):
    lines = []
    code = migrate.main(argv, run=fake, out=lines.append)
    return code, lines


@pytest.fixture()
def fake():
    return FakeRemindctl([reminder("AAA", "大白年检", all_day=True, priority="high", rule=YEARLY),
                          reminder("BBB", "取消Darkroom的订阅", due="2027-10-03T12:00:00Z",
                                   priority="low")])


# ---------------------------------------------------------------- dry-run

def test_dry_run_prints_the_plan_and_touches_nothing(fake):
    code, lines = run_main(["--dry-run"], fake)
    assert code == 0
    assert fake.edits == [], "dry-run 不许调 edit"
    assert all(item["listName"] == "提醒事项" for item in fake.items), "一条都不许动"
    assert lines[0] == "会搬：大白年检 | 2027-03-01 | high | yearly"
    assert lines[1].startswith("会搬：取消Darkroom的订阅 | 2027-10-03")
    assert lines[-1] == "共 2 条（--dry-run：一条都没动）"


def test_dry_run_prints_no_cycle_as_wu(fake):
    one = FakeRemindctl([reminder("CCC", "买菜", due="2027-03-02T12:00:00Z")])
    _code, lines = run_main(["--dry-run"], one)
    assert lines[0].startswith("会搬：买菜 | 2027-03-02 "), lines[0]
    assert lines[0].endswith("| none | 无")


# ---------------------------------------------------------------- apply

def test_apply_moves_each_item_with_the_right_command(fake):
    code, lines = run_main(["--apply"], fake)
    assert code == 0
    assert fake.edits == [["edit", "AAA", "--list", "GTD", "--json"],
                          ["edit", "BBB", "--list", "GTD", "--json"]]
    assert [item["listName"] for item in fake.items] == ["GTD", "GTD"]
    assert "✓ 搬了：大白年检 → GTD" in lines
    assert "✓ 搬了：取消Darkroom的订阅 → GTD" in lines
    assert "成功 2 / 失败 0" in lines


def test_apply_reports_source_and_target_counts(fake):
    _code, lines = run_main(["--apply"], fake)
    assert "提醒事项：2 条 → 0 条" in lines
    assert "GTD：0 条 → 2 条" in lines


def test_apply_keeps_going_after_a_failure_and_exits_1():
    fake = FakeRemindctl([reminder("AAA", "甲"), reminder("BBB", "乙"),
                          reminder("CCC", "丙")], fail_ids={"BBB"})
    code, lines = run_main(["--apply"], fake)
    assert code == 1, "有失败就退出 1"
    assert [call[1] for call in fake.edits] == ["AAA", "BBB", "CCC"], "失败不中断"
    assert any(line.startswith("✗ 失败：乙") for line in lines)
    assert "成功 2 / 失败 1" in lines
    assert [item["listName"] for item in fake.items] == ["GTD", "提醒事项", "GTD"]


def test_limit_moves_only_the_first(fake):
    code, lines = run_main(["--apply", "--limit", "1"], fake)
    assert code == 0 and len(fake.edits) == 1 and fake.edits[0][1] == "AAA"
    assert [item["listName"] for item in fake.items] == ["GTD", "提醒事项"]
    assert "成功 1 / 失败 0" in lines


# ---------------------------------------------------------------- 已完成条目

def test_completed_items_are_skipped_by_default():
    fake = FakeRemindctl([reminder("AAA", "还没做"), reminder("BBB", "早做完了", done=True)])
    _code, lines = run_main(["--dry-run"], fake)
    assert len(lines) == 2 and lines[-1] == "共 1 条（--dry-run：一条都没动）"
    assert lines[0].startswith("会搬：还没做 | 2027-03-01 ")


def test_include_done_moves_completed_too():
    fake = FakeRemindctl([reminder("AAA", "还没做"), reminder("BBB", "早做完了", done=True)])
    code, _lines = run_main(["--apply", "--include-done"], fake)
    assert code == 0
    assert [call[1] for call in fake.edits] == ["AAA", "BBB"]


# ---------------------------------------------------------------- 参数 / 失败路径

def test_missing_source_list_exits_1():
    fake = FakeRemindctl([], lists=("GTD",))
    code, lines = run_main(["--apply", "--from", "不存在的列表"], fake)
    assert code == 1
    assert fake.edits == []
    assert any("读列表「不存在的列表」失败" in line for line in lines)


def test_source_and_target_are_configurable():
    fake = FakeRemindctl([reminder("AAA", "甲", list_name="GTD_TEST")],
                         lists=("GTD_TEST", "GTD"))
    code, _lines = run_main(["--apply", "--from", "GTD_TEST", "--to", "GTD"], fake)
    assert code == 0
    assert fake.edits == [["edit", "AAA", "--list", "GTD", "--json"]]
    assert fake.items[0]["listName"] == "GTD"


def test_empty_source_reports_nothing_to_move():
    fake = FakeRemindctl([], lists=("提醒事项", "GTD"))
    code, lines = run_main(["--apply"], fake)
    assert code == 0 and fake.edits == []
    assert lines == ["「提醒事项」里没有可搬的条目"]
