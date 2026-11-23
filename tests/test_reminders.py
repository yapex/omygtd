"""reminders 适配层验收测试。

纪律（上一轮踩过坑）：Reminder 是全局的，没有「临时库」。
- 一律用 `GTD_TEST`，禁止出现真实列表名
- `_guard_list` 在 pytest 里会拦住真实列表（由 test_guard_* 四连证）
- fixture `real_list` 无条件清理：成功失败都删掉 GTD_TEST
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

import pytest

from gtd.reminders import (FORBIDDEN_IN_TESTS, TEST_LIST, FakeStore,
                           ReminderStore, RemindctlAdapter, _guard_list,
                           _local_stamp, _resolve_exe, schedule)

REMINDCTL = _resolve_exe("remindctl") or "remindctl"
OTHER_LIST = "GTD_TEST_B"          # 也不碰真实列表，只是第二个测试列表
NOW = datetime(2026, 11, 24, 10, 0)


def _cli(args, timeout=30):
    return subprocess.run([REMINDCTL, *args], capture_output=True, text=True,
                          timeout=timeout)


def _list_titles() -> list[str]:
    proc = _cli(["list", "--json"])
    if proc.returncode != 0:
        return []
    data = json.loads(proc.stdout or "[]")
    return [str(item.get("title")) for item in data if isinstance(item, dict)]


@pytest.fixture
def real_list():
    """专用测试列表：先清残留，用完必删（无论用例成败）。"""
    _cli(["list", TEST_LIST, "--delete", "--force"])
    yield TEST_LIST
    _cli(["list", TEST_LIST, "--delete", "--force"])


class _Proc:
    """冒充 subprocess.CompletedProcess。"""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# ---------- 1-6：FakeStore（不碰真实 Reminder） ----------

def test_fake_add_returns_id():
    fake = FakeStore()
    rid = fake.add("买牛奶", NOW, TEST_LIST)
    assert rid and isinstance(rid, str)


def test_fake_add_visible_in_pending():
    fake = FakeStore()
    rid = fake.add("买牛奶", NOW, TEST_LIST)
    assert rid in [item["id"] for item in fake.pending(TEST_LIST)]


def test_fake_complete_hides_from_pending():
    fake = FakeStore()
    rid = fake.add("买牛奶", NOW, TEST_LIST)
    assert fake.complete(rid) is True
    assert rid not in [item["id"] for item in fake.pending(TEST_LIST)]


def test_fake_delete_removes_from_all():
    fake = FakeStore()
    rid = fake.add("买牛奶", NOW, TEST_LIST)
    assert fake.delete(rid) is True
    assert rid not in [item["id"] for item in fake.all()]
    assert fake.delete(rid) is False      # 再删一次：不存在 → False，不抛


def test_fake_filters_by_list_name():
    fake = FakeStore()
    keep = fake.add("留下的", NOW, TEST_LIST)
    fake.add("另一张表的", NOW, OTHER_LIST)
    assert [item["id"] for item in fake.pending(TEST_LIST)] == [keep]


def test_fake_pending_excludes_completed():
    fake = FakeStore()
    done = fake.add("已办", NOW, TEST_LIST)
    todo = fake.add("待办", NOW, TEST_LIST)
    fake.complete(done)
    assert [item["id"] for item in fake.pending(TEST_LIST)] == [todo]


# ---------- 7-8：依赖倒置 ----------

def test_adapter_and_fake_share_protocol():
    assert isinstance(FakeStore(), ReminderStore)
    assert isinstance(RemindctlAdapter(), ReminderStore)


def test_upper_layer_accepts_protocol():
    """上层函数传 fake 也能跑 —— 依赖倒置的实证，一次 subprocess 都不发。"""
    fake = FakeStore()
    rid = schedule(fake, "交材料", NOW, TEST_LIST, advance_minutes=15)
    assert rid
    assert rid in [item["id"] for item in fake.pending(TEST_LIST)]


# ---------- 9-12：守卫 ----------

@pytest.mark.parametrize("name", sorted(FORBIDDEN_IN_TESTS))
def test_guard_blocks_real_lists(monkeypatch, name):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_reminders.py::x (call)")
    with pytest.raises(RuntimeError, match=TEST_LIST):
        _guard_list(name)


def test_guard_allows_test_list(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_reminders.py::x (call)")
    _guard_list(TEST_LIST)          # 不抛即通过


def test_guard_inactive_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _guard_list("GTD")              # 非 pytest 环境不拦（真机要用）


# ---------- 13-14：真实 adapter（只 2 条，必须用 GTD_TEST） ----------

def test_real_ensure_lists_creates_test_list(real_list):
    adapter = RemindctlAdapter(lists=(real_list,))
    adapter.ensure_lists()
    assert real_list in _list_titles()


def test_real_roundtrip(real_list):
    adapter = RemindctlAdapter(lists=(real_list,))
    adapter.ensure_lists()
    due = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
    rid = adapter.add("pytest 往返", due, real_list, advance_minutes=15)
    assert rid, "add 没拿到 id"
    assert rid in [item["id"] for item in adapter.pending(real_list)]
    assert adapter.complete(rid) is True
    assert rid not in [item["id"] for item in adapter.pending(real_list)]


# ---------- 15-16：adapter 参数与失败路径（monkeypatch，不碰真实 Reminder） ----------

def test_adapter_passes_local_time_and_alarm(monkeypatch):
    adapter = RemindctlAdapter()
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return _Proc(json.dumps({"id": "UUID-1", "isCompleted": False}))

    monkeypatch.setattr(adapter, "_run", fake_run)
    got = adapter.add("交材料", NOW, TEST_LIST, advance_minutes=15,
                      note="备注", url="https://example.com/x")
    args = seen["args"]
    assert got == "UUID-1"
    assert args[:2] == ["add", "--title"] and "交材料" in args
    assert args[args.index("--due") + 1] == "2026-11-24 10:00"
    assert args[args.index("--alarm") + 1] == "2026-11-24 09:45"
    assert args[args.index("--list") + 1] == TEST_LIST


def test_adapter_failures_never_raise(monkeypatch):
    adapter = RemindctlAdapter()
    monkeypatch.setattr(adapter, "_run", lambda args: None)     # 超时/不可执行
    assert adapter.add("x", NOW, TEST_LIST) == ""
    assert adapter.complete("abc") is False
    assert adapter.delete("abc") is False
    assert adapter.pending(TEST_LIST) == []
    assert adapter.all() == []


# ---------- 17-23：UTC → 本地（图上一度差 8 小时的回归） ----------
#
# 真实样本：老板 2026-11-23 记的那条，remindctl 给 14:30Z，Reminders.app 显示 22:30。
# 断言写死 22:30 / 04:00 是以本机时区 Asia/Shanghai（UTC+8）为前提。

REAL_UTC = "2026-11-23T14:30:00Z"
REAL_NON_ALL_DAY = {"dueDate": REAL_UTC, "dueDateIsAllDay": False}


def test_local_stamp_real_sample_is_2230():
    """验收样本：14:30Z → 本地 22:30（不是 14:30）。"""
    stamped = _local_stamp(REAL_NON_ALL_DAY["dueDate"])
    assert stamped == "2026-11-23 22:30"
    parsed = datetime.strptime(stamped, "%Y-%m-%d %H:%M")
    assert (parsed.hour, parsed.minute) == (22, 30)


def test_local_stamp_cross_day():
    """UTC 20:00 落本地次日 04:00：跨日必须跟着日期一起走。"""
    assert _local_stamp("2026-11-23T20:00:00Z") == "2026-11-24 04:00"


def test_local_stamp_all_day_keeps_date_drops_time():
    """全天条目：只有日期没有时刻（EventKit 存的是「本地零点的 UTC」16:00Z）。"""
    stamped = _local_stamp("2026-11-24T16:00:00Z", all_day=True)
    assert stamped == "2026-11-25"
    assert ":" not in stamped


def test_local_stamp_naive_input_not_converted_twice():
    """已带本地墙钟的串（FakeStore 的格式）原样返回 —— 不许再换一次。"""
    assert _local_stamp("2026-11-24 10:00") == "2026-11-24 10:00"


def test_local_stamp_junk_passes_through():
    """认不出的串不抛、不猜；空值给空串。"""
    assert _local_stamp("明天早上") == "明天早上"
    assert _local_stamp("") == ""
    assert _local_stamp(None) == ""


def _scho_record(**over):
    """remindctl list --json 的真实记录形状（SCHO 那条，只读、只当样本）。"""
    record = {"alarmDate": REAL_UTC, "creationDate": "2026-11-23T01:19:12Z",
              "dueDate": REAL_UTC, "dueDateIsAllDay": False,
              "id": "96777596-FA37-4D77-8F0D-3252D743BCAA", "isCompleted": False,
              "listName": TEST_LIST, "priority": "none",
              "title": "卖出至少 103000 港币等额的 SCHO，补充账户内的现金"}
    record.update(over)
    return record


@pytest.mark.parametrize("method", ["pending", "all"])
def test_adapter_reads_localize_due_and_alarm(monkeypatch, method):
    """两条读路径（pending / all）都必须过 `_localize`，漏一条就差 8 小时。"""
    adapter = RemindctlAdapter()
    monkeypatch.setattr(adapter, "_run",
                        lambda args: _Proc(json.dumps([_scho_record()])))
    call = (lambda: adapter.pending(TEST_LIST)) if method == "pending" else adapter.all
    item = call()[0]
    assert item["dueDate"] == "2026-11-23 22:30"
    assert item["alarmDate"] == "2026-11-23 22:30"
    assert item["dueDateIsAllDay"] is False


def test_adapter_all_day_record_has_no_time(monkeypatch):
    adapter = RemindctlAdapter()
    record = _scho_record(dueDate="2026-11-24T16:00:00Z", dueDateIsAllDay=True)
    record.pop("alarmDate")
    monkeypatch.setattr(adapter, "_run",
                        lambda args: _Proc(json.dumps([record])))
    item = adapter.all()[0]
    assert item["dueDate"] == "2026-11-25"
    assert "alarmDate" not in item


def test_real_read_write_roundtrip_is_self_consistent(real_list):
    """写读自洽实证：本地 10:00 写给 remindctl（内部转 UTC），读回来还是本地 10:00。

    这是本卡第 3 条要求：写侧 `_stamp` 送本地墙钟、读侧 `_localize` 换回本地，
    两个方向必须互逆。实测 EventKit 侧存的是 UTC（+8 时区的 10:00 → 02:00Z）。
    """
    adapter = RemindctlAdapter(lists=(real_list,))
    adapter.ensure_lists()
    due = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
    rid = adapter.add("pytest 时区往返", due, real_list, advance_minutes=15)
    assert rid
    (item,) = [row for row in adapter.pending(real_list) if row["id"] == rid]
    assert item["dueDate"] == due.strftime("%Y-%m-%d %H:%M")
    assert item["alarmDate"] == (due - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")


# ---------- 24+：全天 / 时段（t_0541f6c3） ----------
#
# 实测真机：remindctl add --due "2026-11-24" --alarm "2026-11-24 12:00"
#   → dueDateIsAllDay: true、alarmDate 仍是那一刻（due 与 alarm 独立，可组合）。

def _add_args(monkeypatch, title="买菜", **kwargs):
    """跑一次 adapter.add（不发真子进程），返回传给 remindctl 的参数表。"""
    adapter = RemindctlAdapter()
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return _Proc(json.dumps({"id": "UUID-1", "isCompleted": False}))

    monkeypatch.setattr(adapter, "_run", fake_run)
    adapter.add(title, NOW, TEST_LIST, **kwargs)
    return seen["args"]


def test_adapter_all_day_sends_date_only(monkeypatch):
    """全天：--due 只给日期，绝不传时刻、也不带 --alarm。"""
    args = _add_args(monkeypatch, all_day=True)
    assert args[args.index("--due") + 1] == "2026-11-24"
    assert "--alarm" not in args


def test_adapter_morning_slot_rings_at_noon(monkeypatch):
    args = _add_args(monkeypatch, slot="am")
    assert args[args.index("--due") + 1] == "2026-11-24"
    assert args[args.index("--alarm") + 1] == "2026-11-24 12:00"


def test_adapter_afternoon_slot_rings_at_six(monkeypatch):
    args = _add_args(monkeypatch, slot="pm")
    assert args[args.index("--due") + 1] == "2026-11-24"
    assert args[args.index("--alarm") + 1] == "2026-11-24 18:00"


def test_adapter_precise_time_rings_at_that_moment(monkeypatch):
    """到点即响（advance=0）：due 与 alarm 都是那一刻。"""
    args = _add_args(monkeypatch, advance_minutes=0)
    assert args[args.index("--alarm") + 1] == "2026-11-24 10:00"


def test_fake_all_day_item_has_flag_and_no_clock():
    fake = FakeStore()
    fake.add("买菜", NOW, TEST_LIST, all_day=True)
    item = fake.all()[0]
    assert item["all_day"] is True and item["dueDateIsAllDay"] is True
    assert item["dueDate"] == "2026-11-24" and ":" not in item["dueDate"]
    assert item["alarmDate"] == ""


def test_read_path_all_day_with_alarm_keeps_local_alarm(monkeypatch):
    """读路径：allDay=true → all_day=True、dueDate 只有日期；alarmDate 换成本地 12:00。"""
    adapter = RemindctlAdapter()
    record = _scho_record(dueDate="2026-11-23T16:00:00Z", dueDateIsAllDay=True,
                          alarmDate="2026-11-24T04:00:00Z")
    monkeypatch.setattr(adapter, "_run", lambda args: _Proc(json.dumps([record])))
    item = adapter.all()[0]
    assert item["all_day"] is True
    assert item["dueDate"] == "2026-11-24"            # 本地日期，不退一天
    assert item["alarmDate"] == "2026-11-24 12:00"    # 04:00Z = 本地 12:00
