"""严格时间入参（``gtd/timeutil.py``）：只认 ISO 日期与 24 小时钟点。

这里锁的不是「怎么说话」，而是「传进来的标准值怎么变成 Reminder 的 due / alarm」。
口语换算（「明天」→ 日期）由调用方 LLM 负责，本模块连试都不试。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from gtd import repeat, timeutil


# ---------------------------------------------------------------- 日期

def test_parse_date_accepts_iso():
    assert timeutil.parse_date("2026-12-01") == date(2026, 12, 1)


@pytest.mark.parametrize("raw", ["明天", "下周", "2026/12/01", "2026-12-1", "12-01",
                                "20261201", ""])
def test_parse_date_rejects_anything_else(raw):
    with pytest.raises(ValueError) as exc:
        timeutil.parse_date(raw)
    assert "YYYY-MM-DD" in str(exc.value) and "例：2026-12-01" in str(exc.value)


def test_parse_date_rejects_an_impossible_day():
    with pytest.raises(ValueError, match="2026-12-01"):
        timeutil.parse_date("2026-02-30")


# ---------------------------------------------------------------- 钟点

@pytest.mark.parametrize("raw,expected", [("00:00", time(0, 0)), ("09:30", time(9, 30)),
                                          ("23:59", time(23, 59))])
def test_parse_clock_accepts_24h_hh_mm(raw, expected):
    assert timeutil.parse_clock(raw) == expected


@pytest.mark.parametrize("raw", ["9点", "9:00", "09:5", "24:00", "9.30", "晚上9点", ""])
def test_parse_clock_rejects_anything_else(raw):
    with pytest.raises(ValueError) as exc:
        timeutil.parse_clock(raw)
    assert "HH:MM" in str(exc.value) and "例：09:30" in str(exc.value)


# ---------------------------------------------------------------- 四种形态

def test_date_only_is_all_day_without_alarm():
    when = timeutil.When(due=date(2026, 12, 1))
    assert when.all_day is True
    assert when.moment == datetime(2026, 12, 1, 0, 0)
    assert when.advance_minutes is None


def test_slot_keeps_all_day_and_has_no_advance():
    when = timeutil.When(due=date(2026, 12, 1), slot="pm")
    assert when.all_day is True and when.slot == "pm" and when.advance_minutes is None


def test_clock_is_the_exact_moment_and_rings_on_time():
    when = timeutil.When(due=date(2026, 12, 1), at=time(9, 30))
    assert when.all_day is False
    assert when.moment == datetime(2026, 12, 1, 9, 30)
    assert when.advance_minutes == 0


def test_no_due_means_no_moment():
    when = timeutil.When()
    assert when.moment is None and when.all_day is False


def test_audit_reports_the_same_shape_as_the_payload():
    when = timeutil.When(due=date(2026, 12, 1), at=time(9, 5))
    assert when.audit() == {"due": "2026-12-01", "at": "09:05",
                            "all_day": False, "slot": None}


# ---------------------------------------------------------------- repeat 映射

@pytest.mark.parametrize("frequency,interval,expected", [
    ("daily", 1, "daily"),
    ("weekly", 1, "weekly"),
    ("monthly", 1, "monthly"),
    ("daily", 3, "every 3 days"),
    ("weekly", 2, "every 2 weeks"),
    ("monthly", 2, "every 2 months"),
])
def test_spec_maps_flags_to_remindctl_writing(frequency, interval, expected):
    assert repeat.spec(frequency, interval) == expected


@pytest.mark.parametrize("frequency,interval", [("yearly", 1), ("biweekly", 1),
                                               ("daily", 0), ("daily", "x"), (None, 1)])
def test_spec_refuses_unknown_input(frequency, interval):
    assert repeat.spec(frequency, interval) is None


def test_weekly_interval_two_reads_back_as_biweekly():
    """写法 ``every 2 weeks`` 与 EventKit 的 ``{weekly, 2}`` 是同一件事。"""
    assert repeat.from_rule({"frequency": "weekly", "interval": 2}) == repeat.BIWEEKLY


def test_every_counted_writing_maps_to_native_frequency_and_interval():
    assert repeat.spec("daily", 5) == "every 5 days"
    assert repeat.from_rule({"frequency": "daily", "interval": 5}) == "every 5 days"
    assert repeat.label(repeat.spec("daily", 5)) == "每 5 天"
