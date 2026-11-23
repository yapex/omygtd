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
                           _resolve_exe, schedule)

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
