"""parser 的验收测试：全部注入固定 now，不依赖系统时钟。

固定基准：2026-11-23（周一）09:00。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from gtd.parser import Parsed, parse

NOW = datetime(2026, 11, 23, 9, 0)          # 周一
TODAY = date(2026, 11, 23)


def p(text, now=NOW):
    return parse(text, now=now)


# ---------- 1-11：日期 / 时刻主体 ----------

def test_today_vegetables():
    """今天买菜 → due=今天, at=22:30, 标题抠干净。"""
    got = p("今天买菜")
    assert got.due == TODAY
    assert got.at == time(22, 30)
    assert got.title == "买菜"


def test_tomorrow_noon_before():
    """明天中午前交材料 → 中午前是截止点 12:00。"""
    got = p("明天中午前交材料")
    assert got.title == "交材料"
    assert got.due == date(2026, 11, 24)
    assert got.at == time(12, 0)


def test_day_after_tomorrow_morning():
    """后天上午开会 → 上午 = 12:00 截止点。"""
    got = p("后天上午开会")
    assert got.due == date(2026, 11, 25)
    assert got.at == time(12, 0)
    assert got.title == "开会"


def test_afternoon_three_oclock():
    """下午三点喝咖啡 → 三点按下午理解。"""
    got = p("下午三点喝咖啡")
    assert got.title == "喝咖啡"
    assert got.at == time(15, 0)


def test_colon_time():
    """15:00 对账 → 时刻原样，空格与标题都干净。"""
    got = p("15:00 对账")
    assert got.at == time(15, 0)
    assert got.title == "对账"


def test_half_past():
    """15点半 → 15:30。"""
    got = p("15点半")
    assert got.at == time(15, 30)
    assert got.title == ""


def test_advance_half_hour():
    """提前半小时提醒我开会 → advance=30，「提醒我」也抠掉。"""
    got = p("提前半小时提醒我开会")
    assert got.advance_minutes == 30
    assert got.title == "开会"


def test_friday_this_week():
    """周五交周报（now 周一）→ 本周五。"""
    got = p("周五交周报")
    assert got.due == date(2026, 11, 27)
    assert got.title == "交周报"


def test_next_monday():
    """下周一复盘 → 下周一。"""
    got = p("下周一复盘")
    assert got.due == date(2026, 11, 30)
    assert got.title == "复盘"


def test_month_day():
    """11月25日体检。"""
    got = p("11月25日体检")
    assert got.due == date(2026, 11, 25)
    assert got.title == "体检"


def test_day_of_month():
    """25号交房租。"""
    got = p("25号交房租")
    assert got.due == date(2026, 11, 25)
    assert got.title == "交房租"


# ---------- 12-15：不解析 / 列表 / 纯函数 ----------

def test_no_time_leaves_title():
    """买鸡蛋 → 全空，标题原样，matched 空串。"""
    got = p("买鸡蛋")
    assert (got.title, got.due, got.at, got.advance_minutes) == ("买鸡蛋", None, None, None)
    assert got.matched == ""
    assert got.list == "GTD"


def test_remind_me_only():
    """提醒我一下 → 没时间，全空。"""
    got = p("提醒我一下")
    assert got.title == ""
    assert got.due is None and got.at is None and got.advance_minutes is None


def test_waiting_list():
    """等对方回设计稿 → Waiting。"""
    got = p("等对方回设计稿")
    assert got.list == "Waiting"
    assert got.title == "回设计稿"


def test_pure_function_same_result():
    """同一输入多次调用结果完全相同。"""
    assert p("明天下午三点开会") == p("明天下午三点开会")


# ---------- 16-20：跨日 / 边界 ----------

def test_tomorrow_at_2359():
    """now 23:59 时「明天」仍是明天。"""
    got = parse("明天", now=datetime(2026, 11, 23, 23, 59))
    assert got.due == date(2026, 11, 24)


def test_day_of_month_already_passed():
    """25号，而 now 已是 26 号 → 下月 25 号。"""
    got = parse("25号", now=datetime(2026, 11, 26, 9, 0))
    assert got.due == date(2026, 12, 25)


def test_bare_three_defaults_afternoon():
    """3点 → 15:00（默认下午）。"""
    assert p("3点").at == time(15, 0)


def test_early_morning_three():
    """凌晨3点 → 03:00。"""
    assert p("凌晨3点").at == time(3, 0)


def test_evening_ten():
    """晚上10点 → 22:00。"""
    assert p("晚上10点").at == time(22, 0)


# ---------- 21-25：标题抠除边界 ----------

def test_duplicate_date():
    """明天明天交材料 → 只留一个语义，标题干净。"""
    got = p("明天明天交材料")
    assert got.due == date(2026, 11, 24)
    assert got.title == "交材料"


def test_duplicate_period():
    """下午下午 → at 取最后一个 18:00，标题空。"""
    got = p("下午下午")
    assert got.at == time(18, 0)
    assert got.title == ""


def test_surrounding_spaces():
    """首尾空格。"""
    assert p("  明天 交材料  ").title == "交材料"


def test_fullwidth_period_punct():
    """全角句号在句尾。"""
    assert p("明天交材料。").title == "交材料"


def test_fullwidth_comma_inside():
    """全角逗号紧邻被抠掉的时间词。"""
    got = p("明天，交材料")
    assert got.title == "交材料"
    assert got.due == date(2026, 11, 24)


# ---------- 26+：补充覆盖面 ----------

def test_early_morning_seven():
    assert p("早上7点出门").at == time(7, 0)


def test_dot_hour():
    assert p("15点对账").at == time(15, 0)


def test_hour_minute_no_colon():
    """15点30 → 15:30。"""
    assert p("15点30").at == time(15, 30)


def test_noon_period():
    assert p("中午吃饭").at == time(12, 0)


def test_big_day_after_tomorrow():
    """大后天 → +3。"""
    assert p("大后天").due == date(2026, 11, 26)


def test_next_sunday():
    """下周日 = 下周一 +6。"""
    assert p("下周日").due == date(2026, 12, 6)


def test_weekday_this_weekend():
    """周六（now 周一）→ 本周六。"""
    assert p("周六").due == date(2026, 11, 28)


def test_weekday_already_passed():
    """周五，而 now 是周六 → 下周周五。"""
    got = parse("周五", now=datetime(2026, 11, 28, 9, 0))
    assert got.due == date(2026, 12, 4)


def test_dash_month_day():
    assert p("11-25交材料").due == date(2026, 11, 25)


def test_slash_month_day():
    assert p("11/25交材料").due == date(2026, 11, 25)


def test_month_day_rolls_to_next_year():
    """11月25日，而 now 已是 12 月 → 明年。"""
    got = parse("11月25日体检", now=datetime(2026, 12, 1, 9, 0))
    assert got.due == date(2027, 11, 25)


def test_advance_hour():
    assert p("提前一小时提醒我开会").advance_minutes == 60


def test_advance_30_minutes():
    assert p("提前30分钟提醒我").advance_minutes == 30


def test_tomorrow_afternoon_three():
    """一句话里多个时间词：due 取明天，at 取最后的三点。"""
    got = p("明天下午三点开会")
    assert got.due == date(2026, 11, 24)
    assert got.at == time(15, 0)
    assert got.title == "开会"


def test_today_with_clock_keeps_clock():
    """今天15点对账 → at 用 15:00，不退回 22:30。"""
    got = p("今天15点对账")
    assert got.due == TODAY
    assert got.at == time(15, 0)
    assert got.title == "对账"


def test_before_clock():
    """12点前交材料 → 「前」是截止语义，标题不留「前」。"""
    got = p("12点前交材料")
    assert got.at == time(12, 0)
    assert got.title == "交材料"


def test_zai_before_clock():
    """在12点之前 → 「在…之前」语义等价，整段抠掉。"""
    got = p("在12点之前交材料")
    assert got.at == time(12, 0)
    assert got.title == "交材料"


def test_matched_echoes_source_text():
    """matched 是原句里的时间原文，便于回显。"""
    assert p("明天下午三点开会").matched == "明天下午三点"
    assert p("提前半小时提醒我开会").matched == "提前半小时"


def test_matched_skips_text_between_time_words():
    """两个时间词中间隔了正文时，matched 用空格分开，不吞正文。"""
    got = p("明天交材料，提前半小时提醒我")
    assert got.matched == "明天 提前半小时"
    assert got.title == "交材料"


def test_trailing_remind_me():
    """句尾的「提醒我」也抠掉，标题不留。"""
    got = p("11月25日去医院体检，提前一小时提醒我")
    assert got.title == "去医院体检"
    assert got.due == date(2026, 11, 25)
    assert got.advance_minutes == 60


def test_title_two_oclock():
    """两点 → 14:00（中文数字 + 默认下午）。"""
    assert p("两点开会").at == time(14, 0)


def test_parsed_is_frozen():
    """Parsed 不可变。"""
    with pytest.raises(Exception):
        Parsed(title="x").title = "y"
