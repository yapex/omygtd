"""提醒事项的四档优先级（与 Reminders.app / remindctl 的原生 priority 一致）。

取值只有四档：``none | low | medium | high``。显示成感叹号，**越重要感叹号越多**
（老板在 Reminders.app 里实测：低=1 个、中=2 个、高=3 个）：
high → ``!!!``、medium → ``!!``、low → ``!``、none → 不印。

本模块只管「词汇 + 排序 + 显示标记」三件事：不读库、不解析参数、不认识渲染格式。
上层（parser / inbox / reminders / render / cli）共用这一份，别在别处再抄一份映射。
"""

from __future__ import annotations

PRIORITY_NONE = "none"
PRIORITY_LOW = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH = "high"
PRIORITIES = (PRIORITY_NONE, PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH)

RANK = {PRIORITY_NONE: 0, PRIORITY_LOW: 1, PRIORITY_MEDIUM: 2, PRIORITY_HIGH: 3}
# 感叹号越多越重要（老板实测：低 1 个 / 中 2 个 / 高 3 个）
MARKS = {PRIORITY_HIGH: "!!!", PRIORITY_MEDIUM: "!!", PRIORITY_LOW: "!"}


def normalize(value) -> str:
    """任何来路的值 → 四档之一；认不出一律 ``none``（宁可少标，不许瞎标）。"""
    text = str(value or "").strip().lower()
    return text if text in RANK else PRIORITY_NONE


def rank(value) -> int:
    """档位高低（0 none → 3 high），排序用。"""
    return RANK[normalize(value)]


def is_set(value) -> bool:
    """不是 ``none`` 才算「标了优先级」。"""
    return normalize(value) != PRIORITY_NONE


def mark(value) -> str:
    """感叹号标记：high → ``!!!``、medium → ``!!``、low → ``!``、none → 空串。"""
    return MARKS.get(normalize(value), "")
