"""严格时间入参：ISO 日期 / 24 小时钟点 → Reminder 需要的字段。

**不在这里做口语解析**：把「明天」换算成 ``2026-12-01`` 是调用方（LLM）的活。
CLI 只认两种形状：

- 日期 ``YYYY-MM-DD``
- 钟点 ``HH:MM``（24 小时制）

时段（``am`` / ``pm``）不是时刻：due 仍是全天，另挂 12:00 / 18:00 的闹钟。
纯函数：不读时钟、不读库、不调网络。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time

LIST_DEFAULT = "GTD"
LIST_WAITING = "Waiting"
LISTS = (LIST_DEFAULT, LIST_WAITING)

SLOT_AM, SLOT_PM = "am", "pm"
SLOTS = (SLOT_AM, SLOT_PM)

DATE_EXAMPLE = "2026-12-01"
CLOCK_EXAMPLE = "09:30"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_date(raw) -> date:
    """``YYYY-MM-DD`` → date；其它一律 ValueError（文案带正确示例）。"""
    text = str(raw or "").strip()
    if not _DATE.fullmatch(text):
        raise ValueError(f"--due 只接受 YYYY-MM-DD（例：{DATE_EXAMPLE}），收到「{raw}」")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"--due 不是有效日期（要 YYYY-MM-DD，例：{DATE_EXAMPLE}），"
                         f"收到「{raw}」") from None


def parse_clock(raw) -> time:
    """``HH:MM``（24 小时制）→ time；``9点`` / ``9:00`` 这类一律 ValueError。"""
    text = str(raw or "").strip()
    if not _CLOCK.fullmatch(text):
        raise ValueError(f"--at 只接受 24 小时制 HH:MM（例：{CLOCK_EXAMPLE}），"
                         f"收到「{raw}」")
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


@dataclass(frozen=True)
class When:
    """一次写入的到期语义：日期 + 可选的钟点或时段。"""

    due: date | None = None
    at: time | None = None
    slot: str | None = None

    @property
    def all_day(self) -> bool:
        """有日期、没钟点 → 全天（时段 am/pm 也是全天，只是另挂闹钟）。"""
        return self.due is not None and self.at is None

    @property
    def moment(self) -> datetime | None:
        """送给存储层的到期时刻；全天只给当天零点占位（读法由 all_day 决定）。"""
        if self.due is None:
            return None
        return datetime.combine(self.due, self.at or time(0, 0))

    @property
    def advance_minutes(self) -> int | None:
        """具体时刻到点即响（0）；全天 / 时段不设提前量（None）。"""
        return 0 if self.at is not None else None

    def audit(self) -> dict:
        """审计与回执共用的结构化口径（标题、优先级由调用方另加）。"""
        return {"due": self.due.isoformat() if self.due else None,
                "at": self.at.strftime("%H:%M") if self.at else None,
                "all_day": self.all_day, "slot": self.slot}
