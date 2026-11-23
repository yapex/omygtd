"""健康检查：**这系统还活着吗**。

四个指标 + 一张人类可读的问题清单（``Health.problems``）。只看不修：

- ``check()`` 纯只读：不写库、不写 Reminder、不重建镜像。
- ``rebuild_mirror()`` 是 ``mirror_drift > 0`` 时的手动修复动作，**只有人喊了才跑**。

数据来源三处：

1. 本地库（``inbox`` / ``mirror``）：用 ``Inbox.oldest_age_days`` 与 ``Mirror.all``。
2. Reminder：``store.all()`` 过滤成 ``GTD`` / ``Waiting`` 里**未完成**的条目。
   上层只认 ``ReminderStore`` Protocol，本模块不认识 remindctl。
3. 出图耗时：``data/board_seconds.json``（``record_board_seconds`` 写，读不到给 None）。

``problems`` 空 = 健康。判定阈值集中在本文件顶部常量，别散落在各处。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import priority as prio
from .inbox import DEFAULT_DB, PROJECT_ROOT, Inbox, Mirror
from .reminders import DEFAULT_LISTS

LAG_LIMIT_DAYS = 14          # 收件箱最老一条超过这么多天算积压
COUNT_LIMIT = 30             # 收件箱条数上限
BOARD_LIMIT_SECONDS = 5.0    # 出图超过这么多秒算慢

STAMP_ENV = "GTD_BOARD_STAMP"
STAMP_NAME = "board_seconds.json"


@dataclass(frozen=True)
class Health:
    """一次健康检查的结果。``problems`` 为空即健康。"""

    inbox_lag_days: int | None      # inbox 里最老一条躺了几天；空表=0
    inbox_count: int                # 未迁出的条数
    reminder_ok: bool               # remindctl 可用且 Full access
    mirror_count: int               # 本地镜像条数
    remote_count: int               # Reminder 里 GTD+Waiting 的条数
    mirror_drift: int               # |mirror_count - remote_count|
    board_seconds: float | None     # 上次生成今日视图耗时（读不到为 None）
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没问题就是活着。"""
        return not self.problems


# ---------------------------------------------------------------- 主入口

def check(*, inbox_db, store, now=None) -> Health:
    """跑一次只读体检。``inbox_db`` 是本地库路径，``store`` 是 Reminder 存储。"""
    inbox = Inbox(inbox_db)
    mirror = Mirror(inbox_db)
    lag = _lag_days(inbox, now)
    count = len(inbox.pending())
    ok = _reminder_ok(store)
    remote = _remote_rows(store) if ok else []
    local = mirror.all()
    seconds = board_seconds()
    drift = _drift(len(local), len(remote), ok)
    return Health(
        inbox_lag_days=lag,
        inbox_count=count,
        reminder_ok=ok,
        mirror_count=len(local),
        remote_count=len(remote),
        mirror_drift=drift,
        board_seconds=seconds,
        problems=_problems(lag, count, ok, drift, seconds),
    )


def rebuild_mirror(*, inbox_db, store, now=None) -> int:
    """按 Reminder 当前状态整表重建本地镜像，返回写入条数。

    手动动作：``check()`` 不会调它。Reminder 不可用时**拒绝重建** —— 否则会拿
    一份空数据把本地备份清掉（那是唯一的本地副本）。
    """
    if not _reminder_ok(store):
        raise RuntimeError("Reminder 不可用，拒绝重建镜像（会把本地备份清空）")
    rows = [row for row in (_mirror_row(item) for item in _remote_rows(store))
            if row["reminder_id"]]
    return Mirror(inbox_db).replace_all(rows, now=now)


# ---------------------------------------------------------------- 取数

def _lag_days(inbox: Inbox, now) -> int | None:
    """最老一条念头的天数；空表 = 0（没积压），未来时间也不会给负数。"""
    lag = inbox.oldest_age_days(now=now)
    return 0 if lag is None else max(0, lag)


def _remote_rows(store) -> list[dict]:
    """Reminder 里 ``GTD`` / ``Waiting`` 未完成的条目（一张表才算得准）。"""
    names = set(DEFAULT_LISTS)
    return [item for item in store.all()
            if str(item.get("listName")) in names and not item.get("isCompleted")]


def _mirror_row(item: dict) -> dict:
    """remindctl / FakeStore 的条目 → ``Mirror.replace_all`` 认的行。"""
    return {
        "reminder_id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "list_name": str(item.get("listName") or ""),
        "due_at": item.get("dueDate") or None,
        "completed": int(bool(item.get("isCompleted"))),
        "priority": prio.normalize(item.get("priority")),
        "synced_at": None,          # 让 replace_all 用注入的 now 盖章
    }


def _reminder_ok(store) -> bool:
    """可用性由存储自己回答：有 ``is_available()`` 就信它。

    纯内存实现（FakeStore）也实现了探针（默认 True），所以这里只剩一条路；
    没有这个方法的鸭子类型实现视为可用。
    """
    probe = getattr(store, "is_available", None)
    return bool(probe()) if callable(probe) else True


def _drift(local_count: int, remote_count: int, reminder_ok: bool) -> int:
    """差几条；Reminder 不可用时不算（拿 0 条去比只会得出假差值）。"""
    return abs(local_count - remote_count) if reminder_ok else 0


# ---------------------------------------------------------------- 出图耗时

def board_stamp_path() -> Path:
    """耗时记录文件：``GTD_BOARD_STAMP`` > ``data/board_seconds.json``。"""
    raw = os.environ.get(STAMP_ENV)
    return Path(raw).expanduser() if raw else PROJECT_ROOT / "data" / STAMP_NAME


def board_seconds() -> float | None:
    """上次出图耗时；文件缺 / 坏 / 字段不对一律 None（不猜）。"""
    try:
        data = json.loads(board_stamp_path().read_text(encoding="utf-8"))
        return float(data["seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_board_seconds(seconds, *, now=None) -> Path | None:
    """记一次出图耗时。

    pytest 里默认不写（不许把测试噪声灌进真实 ``data/``），要测就显式设
    ``GTD_BOARD_STAMP`` 指到临时文件。
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(STAMP_ENV):
        return None
    path = board_stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = {"seconds": round(float(seconds), 3),
             "at": (now or datetime.now()).isoformat(timespec="seconds")}
    path.write_text(json.dumps(stamp, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 问题清单

def _problems(lag: int | None, count: int, reminder_ok: bool,
              drift: int, seconds: float | None) -> list[str]:
    """五条规则，逐条独立判；成立就列，不成立一个字都不加。"""
    items: list[str] = []
    if lag is not None and lag > LAG_LIMIT_DAYS:
        items.append(f"收件箱积压 {lag} 天，该清了")
    if not reminder_ok:
        items.append("Reminder 不可用，任务写不进去")
    if reminder_ok and drift > 0:      # Reminder 挂了时差值是假的，不喊人去重建
        items.append(f"本地镜像与 Reminder 差 {drift} 条，建议重建")
    if seconds is not None and seconds > BOARD_LIMIT_SECONDS:
        items.append(f"出图耗时 {_fmt_seconds(seconds)} 秒，偏慢")
    if count > COUNT_LIMIT:
        items.append(f"收件箱 {count} 条，超过 {COUNT_LIMIT} 条说明没在整理")
    return items


def _fmt_seconds(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")
