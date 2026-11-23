"""Reminder 适配层：全系统唯一碰「提醒事项」的地方。

依赖倒置：上层只认 `ReminderStore` 这个 Protocol，不认识 remindctl，
也不知道 subprocess 长什么样。真实实现 `RemindctlAdapter` 与测试实现
`FakeStore` 可互换 —— 想换掉 remindctl（换工具、换平台）只改本文件。

纪律：
- 任何失败一律返回 "" / False / 空列表，**绝不抛** —— GTD 主流程不许被闹钟拖死
- 每次 subprocess 都带硬超时：无 TCC 授权时 remindctl 会挂死
- 写操作必须先过 `_guard_list`：pytest 里禁止碰真实列表（GTD / Waiting / 提醒事项）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

DEFAULT_LISTS = ("GTD", "Waiting")
TEST_LIST = "GTD_TEST"
FORBIDDEN_IN_TESTS = {"GTD", "Waiting", "提醒事项"}

REMINDCTL_TIMEOUT = 20
FALLBACK_PATHS = ("~/.local/bin/remindctl", "/opt/homebrew/bin/remindctl",
                  "/usr/local/bin/remindctl")

TIME_FMT = "%Y-%m-%d %H:%M"


# ---------------------------------------------------------------- 守卫

def _guard_list(name: str) -> None:
    """pytest 里只许写 GTD_TEST：Reminder 是全局的，没有「临时库」可用。"""
    if os.environ.get("PYTEST_CURRENT_TEST") and name in FORBIDDEN_IN_TESTS:
        raise RuntimeError(
            f"测试环境禁止写真实列表：{name}。测试请用 {TEST_LIST}"
        )


# ---------------------------------------------------------------- 抽象

@runtime_checkable
class ReminderStore(Protocol):
    """提醒事项存储的抽象。上层函数只许依赖这个类型。"""

    def is_available(self) -> bool:
        """底层工具可用且已授权；探测失败返回 False，绝不抛。"""
        ...

    def ensure_lists(self) -> None:
        """确保自己负责的列表都存在。"""
        ...

    def add(self, title: str, due_at: datetime, list_name: str,
            advance_minutes: int | None = None,
            note: str = "", url: str = "") -> str:
        """新建一条提醒，返回 id；失败返回 ""。"""
        ...

    def complete(self, reminder_id: str) -> bool:
        """标记完成；失败返回 False。"""
        ...

    def delete(self, reminder_id: str) -> bool:
        """删除；失败返回 False。"""
        ...

    def pending(self, list_name: str) -> list[dict]:
        """该列表里未完成的提醒。"""
        ...

    def all(self) -> list[dict]:
        """所有列表的所有提醒（含已完成）。"""
        ...


# ---------------------------------------------------------------- 工具函数

def _stamp(moment) -> str:
    """datetime → remindctl 认的本地时间（分钟级）；EventKit 内部存 UTC，工具负责换。"""
    if isinstance(moment, datetime):
        return moment.strftime(TIME_FMT)
    return str(moment)


def _parse_utc(value) -> datetime | None:
    """ISO 8601 UTC 串（带 ``Z`` / ``+00:00``）→ 本地时区 aware datetime。

    认不出给 None；没有时区标的串视为「已经是本地时间」，原样返回 naive
    datetime —— 绝不能再当 UTC 换一次，否则反向差 8 小时。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is None else dt.astimezone()


def _local_stamp(value, *, all_day: bool = False) -> str:
    """remindctl 读出的 UTC 时间串 → 本地墙钟串（与 `_stamp` 写出去的格式同构）。

    - 非全天：``"YYYY-MM-DD HH:MM"``，例：``2026-11-23T14:30:00Z`` → ``2026-11-23 22:30``
    - 全天：只留本地日期 ``"YYYY-MM-DD"``（全天没有时刻）。实测 EventKit 把全天
      存成「本地零点的 UTC」，``--due 2026-11-25`` 读回来是 ``2026-11-24T16:00:00Z``，
      所以日期必须先换本地再截，否则日期会退一天。
    - 认不出的串原样返回（不抛、不猜）。
    """
    dt = _parse_utc(value)
    if dt is None:
        return value if isinstance(value, str) else ""
    return dt.strftime("%Y-%m-%d") if all_day else dt.strftime(TIME_FMT)


def _localize(item: dict) -> dict:
    """一条 remindctl 记录：``dueDate`` / ``alarmDate`` 换成本地时间，其余字段不动。

    读侧的唯一出口。EventKit 存 UTC，remindctl JSON 原样给 UTC；写侧 `_stamp`
    送的是本地墙钟（实测 ``--due "10:00"`` → ``dueDate: 02:00Z``），两个方向靠
    这一对函数自洽。**任何新加的读取路径都必须过这里**，漏一处图上就差 8 小时。
    """
    out = dict(item)
    all_day = bool(item.get("dueDateIsAllDay"))
    for key in ("dueDate", "alarmDate"):
        if out.get(key):
            out[key] = _local_stamp(out[key], all_day=(key == "dueDate" and all_day))
    return out


def _resolve_exe(exe: str) -> str | None:
    """remindctl 路径：PATH 里找不到就兜底几个常见安装位置。

    launchd 的 PATH 常只有 /usr/bin:/bin:/usr/sbin:/sbin，靠 which 会失手。
    """
    if os.sep in exe:
        return exe if os.access(exe, os.X_OK) else None
    found = shutil.which(exe)
    if found:
        return found
    for raw in FALLBACK_PATHS:
        path = os.path.expanduser(raw)
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def _ok(proc) -> bool:
    return proc is not None and proc.returncode == 0


def _out(proc) -> str:
    return getattr(proc, "stdout", "") or ""


def _loads(text):
    try:
        return json.loads(text or "")
    except ValueError:
        return None


def _records(data) -> list[dict]:
    """remindctl 的 JSON 输出：可能是单条 dict，也可能是 list。"""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _id_of(data) -> str:
    for item in _records(data):
        if item.get("id"):
            return str(item["id"])
    return ""


# ---------------------------------------------------------------- 真实实现

class RemindctlAdapter:
    """调 remindctl 子进程的真实实现。"""

    def __init__(self, exe: str = "remindctl", timeout: int = REMINDCTL_TIMEOUT,
                 lists=DEFAULT_LISTS):
        self._exe = _resolve_exe(exe)
        self._timeout = timeout
        self._lists = tuple(lists)

    def ensure_lists(self) -> None:
        for name in self._lists:
            _guard_list(name)
            if name not in self._list_names():
                self._run(["list", name, "--create", "--force"])

    def _list_names(self) -> list[str]:
        proc = self._run(["list", "--json"])
        if not _ok(proc):
            return []
        return [str(item.get("title")) for item in _records(_loads(_out(proc)))]

    def add(self, title: str, due_at: datetime, list_name: str,
            advance_minutes: int | None = None,
            note: str = "", url: str = "") -> str:
        """新建一条提醒，返回 id；失败返回 ""。

        `advance_minutes`：提前几分钟响铃（alarm = due_at - advance）；
        传 0 = 到点即响，传 None = 不设闹钟（--alarm 不出现）。
        """
        _guard_list(list_name)
        args = ["add", "--title", title, "--list", list_name, "--due", _stamp(due_at)]
        if advance_minutes is not None:
            alarm = due_at - timedelta(minutes=int(advance_minutes))
            args += ["--alarm", _stamp(alarm)]
        if note:
            args += ["--notes", note]
        if url:
            args += ["--url", url]
        proc = self._run(args + ["--json"])
        if not _ok(proc):
            return ""
        return _id_of(_loads(_out(proc)))

    def complete(self, reminder_id: str) -> bool:
        proc = self._run(["complete", str(reminder_id), "--json"])
        if not _ok(proc):
            return False
        data = _loads(_out(proc))
        if data is None:
            return True          # 输出不可解析时以 rc 为准
        return any(item.get("isCompleted") for item in _records(data))

    def delete(self, reminder_id: str) -> bool:
        proc = self._run(["delete", str(reminder_id), "--force", "--json"])
        return _ok(proc)

    def pending(self, list_name: str) -> list[dict]:
        _guard_list(list_name)
        proc = self._run(["show", "open", "--list", list_name, "--json"])
        if not _ok(proc):
            return []
        return [_localize(item) for item in _records(_loads(_out(proc)))
                if not item.get("isCompleted")]

    def all(self) -> list[dict]:
        proc = self._run(["show", "all", "--json"])
        if not _ok(proc):
            return []
        return [_localize(item) for item in _records(_loads(_out(proc)))]

    def is_available(self) -> bool:
        """remindctl 存在且 status 为 Full access。带超时，失败返回 False。"""
        proc = self._run(["status"])
        if not _ok(proc):
            return False
        return "full access" in _out(proc).lower()

    def _run(self, args):
        """跑一次 remindctl；超时 / 异常 / 不可执行 一律返回 None。"""
        if not self._exe:
            return None
        try:
            return subprocess.run([self._exe, *args], capture_output=True,
                                  text=True, timeout=self._timeout)
        except Exception:      # noqa: BLE001 —— TimeoutExpired / OSError 全兜
            return None


# ---------------------------------------------------------------- 测试实现

class FakeStore:
    """纯内存实现，不碰真实 Reminder；行为与 adapter 对齐，用来测上层。"""

    def __init__(self, lists=(TEST_LIST,), available: bool = True):
        self._lists = tuple(lists)
        self._available = bool(available)
        self._items: dict[str, dict] = {}
        self._seq = 0

    def is_available(self) -> bool:
        return self._available

    def ensure_lists(self) -> None:
        for name in self._lists:
            _guard_list(name)

    def add(self, title: str, due_at: datetime, list_name: str,
            advance_minutes: int | None = None,
            note: str = "", url: str = "") -> str:
        _guard_list(list_name)
        self._seq += 1
        reminder_id = f"fake-{self._seq:04d}"
        alarm = None
        if advance_minutes is not None and isinstance(due_at, datetime):
            alarm = due_at - timedelta(minutes=int(advance_minutes))
        self._items[reminder_id] = {
            "id": reminder_id,
            "title": title,
            "listName": list_name,
            "dueDate": _stamp(due_at),
            "alarmDate": _stamp(alarm) if alarm else "",
            "notes": note,
            "url": url,
            "isCompleted": False,
        }
        return reminder_id

    def complete(self, reminder_id: str) -> bool:
        item = self._items.get(str(reminder_id))
        if item is None:
            return False
        item["isCompleted"] = True
        return True

    def delete(self, reminder_id: str) -> bool:
        return self._items.pop(str(reminder_id), None) is not None

    def pending(self, list_name: str) -> list[dict]:
        _guard_list(list_name)
        return [dict(item) for item in self._items.values()
                if item["listName"] == list_name and not item["isCompleted"]]

    def all(self) -> list[dict]:
        return [dict(item) for item in self._items.values()]


# ---------------------------------------------------------------- 上层示例

def schedule(store: ReminderStore, title: str, due_at: datetime,
             list_name: str = "GTD",
             advance_minutes: int | None = None) -> str:
    """上层函数的样板：只依赖 Protocol，不依赖具体实现。

    真实运行时传 RemindctlAdapter，测试时传 FakeStore，行为一致。
    """
    store.ensure_lists()
    return store.add(title, due_at, list_name, advance_minutes)
