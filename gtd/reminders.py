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
from datetime import date, datetime, time, timedelta
from typing import Protocol, runtime_checkable

from . import priority as prio
from . import repeat as rep
from .priority import PRIORITY_NONE

DEFAULT_LISTS = ("GTD", "Waiting")
TEST_LIST = "GTD_TEST"
FORBIDDEN_IN_TESTS = {"GTD", "Waiting", "提醒事项"}

REMINDCTL_TIMEOUT = 20
FALLBACK_PATHS = ("~/.local/bin/remindctl", "/opt/homebrew/bin/remindctl",
                  "/usr/local/bin/remindctl")

TIME_FMT = "%Y-%m-%d %H:%M"
DAY_FMT = "%Y-%m-%d"
# 时段标记 → 闹钟时刻：due 走全天（整天挂锁屏），到点响一次
SLOT_ALARMS = {"am": time(12, 0), "pm": time(18, 0)}


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

    def add(self, title: str, due_at, list_name: str,
            advance_minutes: int | None = None,
            note: str = "", url: str = "",
            all_day: bool = False, slot: str | None = None,
            priority: str = PRIORITY_NONE,
            repeat: str | None = None) -> str:
        """新建一条提醒，返回 id；失败返回 ""。

        ``all_day=True`` → due 只给日期（整天挂锁屏）；``slot``（am / pm）→ 同样
        due 全天，另外挂 12:00 / 18:00 的闹钟到点响一次。``priority`` 是 Reminders.app
        的原生四档（none/low/medium/high），写进 Reminder 后手机上也能看到感叹号。
        ``repeat`` 是 Reminders 原生重复（``daily`` / ``every 5 days`` …），写进去后
        完成那一刻 EventKit 自己把下一次（原 id、due +N）滚出来。
        """
        ...

    def complete(self, reminder_id: str) -> bool:
        """标记完成；失败返回 False。"""
        ...

    def set_priority(self, reminder_id: str, level: str) -> bool:
        """改一条提醒的优先级（none/low/medium/high）；失败返回 False。

        ``none`` 也是合法值（等于清掉标记），所以四档全传——与 ``add`` 不同。
        """
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


def _day_of(moment) -> date:
    """``datetime`` / ``date`` 一律取日期部分（全天条目只要日期）。"""
    return moment.date() if isinstance(moment, datetime) else moment


def _reminder_when(due_at, *, all_day: bool, slot: str | None):
    """算出 ``--due`` 串与闹钟：→ (due 串, alarm datetime | None, 是否全天)。

    - 全天（``all_day``）或时段（``slot``）→ due 只送日期，EventKit 存 ``dueDateIsAllDay``
    - ``slot=am/pm`` → 当天 12:00 / 18:00 响一次；due 仍是全天
    - 其余 → due 送本地墙钟，闹钟由 ``advance_minutes`` 决定（见调用方）
    """
    alarm = None
    if slot in SLOT_ALARMS:
        alarm = datetime.combine(_day_of(due_at), SLOT_ALARMS[slot])
    if all_day or alarm is not None:
        return _day_of(due_at).strftime(DAY_FMT), alarm, True
    return _stamp(due_at), None, False


def _alarm_of(due_at, advance_minutes: int | None, *, day_only: bool, alarm):
    """闹钟最终值：时段/全天自带的优先；否则按「提前 N 分钟」从 due 倒推。

    ``None`` = 不响（不传 ``--alarm``）。全天条目没有钟点，提前量无从算起 → 忽略。
    """
    if alarm is not None or advance_minutes is None:
        return alarm
    if day_only or not isinstance(due_at, datetime):
        return None
    return due_at - timedelta(minutes=int(advance_minutes))


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
    out["all_day"] = all_day            # 上层只认这个标记：全天条目「没有时刻」
    out["priority"] = prio.normalize(item.get("priority"))   # 四档归一，缺省 none
    out["repeat"] = rep.from_rule(item.get("recurrenceRule"))  # 周期归一，一次性 = None
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


def _completed_or_rolled(item: dict, reminder_id: str) -> bool:
    """这一条是「真完成了」还是「周期任务被滚到下一轮」——两者都算 complete 成功。

    周期任务完成时 remindctl 返回同 id、isCompleted=false、已 +N 的那条；普通任务
    返回同 id、isCompleted=true。既没完成、又没有周期规则的，一律当失败。
    """
    if item.get("isCompleted"):
        return True
    return (str(item.get("id")) == str(reminder_id)
            and bool(item.get("recurrenceRule") or item.get("repeat")))


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

    def add(self, title: str, due_at, list_name: str,
            advance_minutes: int | None = None,
            note: str = "", url: str = "",
            all_day: bool = False, slot: str | None = None,
            priority: str = PRIORITY_NONE,
            repeat: str | None = None) -> str:
        """新建一条提醒，返回 id；失败返回 ""。

        `advance_minutes`：提前几分钟响铃（alarm = due_at - advance）；
        传 0 = 到点即响，传 None = 不设闹钟（--alarm 不出现）。
        `all_day` / `slot`：due 只送日期（全天）；`slot=am/pm` 再挂 12:00/18:00 闹钟。
        `priority`：none/low/medium/high，非 none 才传 ``--priority``（与 Reminders.app 同档）。
        `repeat`：Reminders 原生重复，认得才传 ``--repeat``（不认得就不传）。
        """
        _guard_list(list_name)
        due_text, alarm, day_only = _reminder_when(due_at, all_day=all_day, slot=slot)
        alarm = _alarm_of(due_at, advance_minutes, day_only=day_only, alarm=alarm)
        args = ["add", "--title", title, "--list", list_name, "--due", due_text]
        if alarm is not None:
            args += ["--alarm", _stamp(alarm)]
        if note:
            args += ["--notes", note]
        if url:
            args += ["--url", url]
        if prio.is_set(priority):
            args += ["--priority", prio.normalize(priority)]
        if rep.is_set(repeat):
            args += ["--repeat", rep.normalize(repeat)]
        proc = self._run(args + ["--json"])
        if not _ok(proc):
            return ""
        return _id_of(_loads(_out(proc)))

    def complete(self, reminder_id: str) -> bool:
        """标记完成；**周期任务**完成那一刻 EventKit 会把同一条滚到下个周期。

        实测（t_82fc1c49，真机）：对 ``--repeat`` 的提醒调 complete，返回的是
        **同 id、isCompleted=false、dueDate 已 +N** 的那条（另生成一条已完成的历史
        快照，新 id）。所以「成功」不能只看 isCompleted —— 同 id 且带周期规则的滚动
        也算完成；不带周期的原样返回仍算失败。
        """
        proc = self._run(["complete", str(reminder_id), "--json"])
        if not _ok(proc):
            return False
        data = _loads(_out(proc))
        if data is None:
            return True          # 输出不可解析时以 rc 为准
        return any(_completed_or_rolled(item, reminder_id) for item in _records(data))

    def delete(self, reminder_id: str) -> bool:
        proc = self._run(["delete", str(reminder_id), "--force", "--json"])
        return _ok(proc)

    def set_priority(self, reminder_id: str, level: str) -> bool:
        """``remindctl edit <id> --priority <level>``；rc 为准，输出不可解析也算成功。"""
        args = ["edit", str(reminder_id), "--priority", prio.normalize(level), "--json"]
        return _ok(self._run(args))

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

    def add(self, title: str, due_at, list_name: str,
            advance_minutes: int | None = None,
            note: str = "", url: str = "",
            all_day: bool = False, slot: str | None = None,
            priority: str = PRIORITY_NONE,
            repeat: str | None = None) -> str:
        _guard_list(list_name)
        self._seq += 1
        reminder_id = f"fake-{self._seq:04d}"
        due_text, alarm, day_only = _reminder_when(due_at, all_day=all_day, slot=slot)
        alarm = _alarm_of(due_at, advance_minutes, day_only=day_only, alarm=alarm)
        self._items[reminder_id] = {
            "id": reminder_id,
            "title": title,
            "listName": list_name,
            "dueDate": due_text,              # 全天条目只有日期（与 adapter 读侧同构）
            "dueDateIsAllDay": day_only,
            "all_day": day_only,
            "alarmDate": _stamp(alarm) if alarm else "",
            "notes": note,
            "url": url,
            "priority": prio.normalize(priority),
            "repeat": rep.normalize(repeat) or "",
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

    def set_priority(self, reminder_id: str, level: str) -> bool:
        item = self._items.get(str(reminder_id))
        if item is None:
            return False
        item["priority"] = prio.normalize(level)
        return True

    def pending(self, list_name: str) -> list[dict]:
        _guard_list(list_name)
        return [dict(item) for item in self._items.values()
                if item["listName"] == list_name and not item["isCompleted"]]

    def all(self) -> list[dict]:
        return [dict(item) for item in self._items.values()]


# ---------------------------------------------------------------- 上层示例

def schedule(store: ReminderStore, title: str, due_at,
             list_name: str = "GTD",
             advance_minutes: int | None = None,
             all_day: bool = False, slot: str | None = None,
             priority: str = PRIORITY_NONE,
             repeat: str | None = None) -> str:
    """上层函数的样板：只依赖 Protocol，不依赖具体实现。

    真实运行时传 RemindctlAdapter，测试时传 FakeStore，行为一致。
    """
    store.ensure_lists()
    return store.add(title, due_at, list_name, advance_minutes,
                     all_day=all_day, slot=slot, priority=priority, repeat=repeat)
