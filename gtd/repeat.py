"""重复规则：Reminders.app 原生 ``recurrenceRule`` 的单一真源。

remindctl 的 ``--repeat`` 取值：``daily | weekly | biweekly | monthly | yearly |
every N days/weeks/months/years``。实测（2026-11-23，真机建 ZZ 测试条读回）：

- ``--repeat "every 5 days"`` → ``{"frequency": "daily", "interval": 5}``
- ``biweekly`` 与 ``every 2 weeks`` → 都是 ``{"frequency": "weekly", "interval": 2}``
- ``monthly`` → ``{"frequency": "monthly", "interval": 1}``

所以「写法」与「EventKit 规则」是两个面向：parser 把人话变成写法，adapter 把写法
交给 remindctl，读侧把规则折回写法给视图。三处都 import 这里，别各抄一份映射。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
BIWEEKLY = "biweekly"
NAMED_LABELS = {"daily": "每天", "weekly": "每周", "biweekly": "每两周",
                "monthly": "每月", "yearly": "每年"}
UNIT_LABELS = {"days": "天", "weeks": "周", "months": "个月", "years": "年"}
RULE_UNITS = {"daily": "days", "weekly": "weeks", "monthly": "months", "yearly": "years"}
NAMED_STEPS = {"daily": (1, "days"), "weekly": (7, "days"),
               "monthly": (1, "months"), "yearly": (1, "years")}
_EVERY = re.compile(r"every\s+(?P<n>\d+)\s+(?P<unit>days|weeks|months|years)")


def _every(value: str) -> tuple[int, str] | None:
    """``every 5 days`` → ``(5, "days")``；不是这个形状给 None。"""
    found = _EVERY.fullmatch(value.strip().lower())
    if found is None:
        return None
    count = int(found.group("n"))
    return (count, found.group("unit")) if count > 0 else None


def normalize(value) -> str | None:
    """任意输入 → 规范写法（``biweekly`` / ``every 5 days`` / …）；认不出给 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text in FREQUENCIES or text == BIWEEKLY:
        return text
    counted = _every(text)
    return None if counted is None else f"every {counted[0]} {counted[1]}"


def is_set(value) -> bool:
    """这条重复写法认不认得（不认得就不往 remindctl 传）。"""
    return normalize(value) is not None


def label(value) -> str:
    """写法 → 老板看的文字（``every 5 days`` → ``每 5 天``）；一次性任务给空串。"""
    text = normalize(value)
    if text is None:
        return ""
    if text in NAMED_LABELS:
        return NAMED_LABELS[text]
    counted = _every(text)
    return "" if counted is None else f"每 {counted[0]} {UNIT_LABELS[counted[1]]}"


def from_rule(rule) -> str | None:
    """EventKit 的 ``recurrenceRule`` → 写法；没有规则或认不出给 None。"""
    if not isinstance(rule, dict):
        return None
    frequency = str(rule.get("frequency") or "").strip().lower()
    if frequency not in FREQUENCIES:
        return None
    try:
        interval = int(rule.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 1
    if frequency == "weekly" and interval == 2:
        return BIWEEKLY
    if interval <= 1:
        return frequency
    return f"every {interval} {RULE_UNITS[frequency]}"


def _add_months(day: date, months: int) -> date:
    """加日历月；目标月没有那一天就落到月末（1-31 加一个月 → 2-28/29）。"""
    years, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def next_due(value, today: date) -> date | None:
    """没听出具体日期时的兜底到期日：按重复周期往后推一轮。"""
    text = normalize(value)
    if text is None:
        return None
    if text == BIWEEKLY:
        count, unit = 14, "days"
    elif text in NAMED_STEPS:
        count, unit = NAMED_STEPS[text]
    else:
        found = _every(text)
        if found is None:
            return None
        count, unit = found
    if unit == "days":
        return today + timedelta(days=count)
    if unit == "weeks":
        return today + timedelta(weeks=count)
    return _add_months(today, count * 12 if unit == "years" else count)
