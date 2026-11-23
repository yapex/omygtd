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
    """今天买菜 → due=今天、没有钟点 → 全天（t_0541f6c3 起不再兜底 22:30）。"""
    got = p("今天买菜")
    assert got.due == TODAY
    assert got.at is None and got.slot is None
    assert got.all_day is True
    assert got.title == "买菜"


def test_tomorrow_noon_before():
    """明天中午前交材料 → 中午前是截止点 12:00。"""
    got = p("明天中午前交材料")
    assert got.title == "交材料"
    assert got.due == date(2026, 11, 24)
    assert got.at == time(12, 0)


def test_day_after_tomorrow_morning():
    """后天上午开会 → 上午 = 全天 + 12:00 响（slot=am，不产时刻）。"""
    got = p("后天上午开会")
    assert got.due == date(2026, 11, 25)
    assert got.at is None and got.slot == "am"
    assert got.all_day is False
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
    """下午下午 → 只记一个时段（slot=pm，不产时刻），标题空。"""
    got = p("下午下午")
    assert got.at is None and got.slot == "pm"
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


# ---------- 48+：时间词带空格 / 句首连续吞并（t_707f252b） ----------
#
# 事故：`今天晚上 10 点半 卖出 X` 只剥了「今天晚上」，「10 点半」留在标题里。
# 根因：时间词之间允许空格，但单个词表项匹配不到带空格的整段。
# 口径：空格（半角/全角）一律当可选连接符；句首连续的时间成分整段吃掉，
#       `matched` 回显被吃掉的原文（含空格）。

SPACED_PREFIX = [
    ("今天晚上 10 点半 卖出 X", "卖出 X", TODAY, time(22, 30), "今天晚上 10 点半"),
    ("今天晚上10点半 卖出 X", "卖出 X", TODAY, time(22, 30), "今天晚上10点半"),
    ("今夜10:30 卖出 X", "卖出 X", TODAY, time(22, 30), "今夜10:30"),
    ("今天 晚上 10 点 30 分 卖出 X", "卖出 X", TODAY, time(22, 30),
     "今天 晚上 10 点 30 分"),
    ("明早 8 点 提醒我交材料", "交材料", date(2026, 11, 24), time(8, 0), "明早 8 点"),
    ("后天下午 3 点半 开会", "开会", date(2026, 11, 25), time(15, 30), "后天下午 3 点半"),
    ("下午三点半 开会", "开会", None, time(15, 30), "下午三点半"),
    # 没有日期词时 parser 不自己造日期，due 留 None；CLI 的 _due_at 用 now.date() 兜底成今天
    ("10点半 卖出 X", "卖出 X", None, time(22, 30), "10点半"),
    # 整句都是时间词 → 抠完为空串，与既有 test_half_past（「15点半」→ ""）同一约定
    ("晚上10点半", "", None, time(22, 30), "晚上10点半"),
]


@pytest.mark.parametrize("text,title,due,at,matched", SPACED_PREFIX)
def test_spaced_time_prefix_is_eaten_whole(text, title, due, at, matched):
    """时间词之间带空格也要整段吃掉，matched 回显含空格的原文。"""
    got = p(text)
    assert got.title == title
    assert got.due == due
    assert got.at == at
    assert got.matched == matched


def test_fullwidth_space_between_time_words():
    """全角空格（U+3000）同样是可选连接符。"""
    got = p("今天晚上\u300010\u3000点半\u3000卖出 X")
    assert got.title == "卖出 X"
    assert got.at == time(22, 30)
    assert got.matched == "今天晚上\u300010\u3000点半"


def test_spaced_prefix_absorbs_every_following_time_word():
    """句首连续吞并：每个交界处都隔着空格也要一直吃到正文为止。"""
    got = p("明天 中午前 交材料")
    assert got.title == "交材料"
    assert got.due == date(2026, 11, 24)
    assert got.at == time(12, 0)
    assert got.matched == "明天 中午前"


def test_prefix_across_punctuation_is_eaten():
    """时间前缀之间夹标点也算连续（旧的「明天，交材料」同一族）。"""
    got = p("明天，中午前交材料")
    assert got.title == "交材料"
    assert got.at == time(12, 0)


def test_interior_time_is_not_stripped():
    """正文中间提到的时间不剥 —— 只剥句首连续的时间前缀。"""
    got = p("明天中午前交材料，顺便看看 3 点的会")
    assert got.title == "交材料，顺便看看 3 点的会"
    assert got.due == date(2026, 11, 24)
    # 注（t_93aaa2eb 起）：at 只认句首前缀 → 12:00，正文里的「3 点」不再参与时刻解析


# ---------- 61+：句中时间不许参与时刻解析（t_93aaa2eb） ----------
#
# 事故：上一条只锁了 title，没锁 at。`明天中午前交材料，顺便看看 3 点的会`
#      标题是对的，at 却被正文里的「3 点」抢成 15:00（应为中午前 12:00）。
# 口径：时刻只认**句首时间前缀**里的最后一个；正文中间的时间既不剥、也不算。
#      前缀只给日期不给钟点 → 没有钟点（全天），不再兜底 22:30（t_0541f6c3）。

INTERIOR_TIME = [
    ("明天中午前交材料，顺便看看 3 点的会", "交材料，顺便看看 3 点的会",
     date(2026, 11, 24), time(12, 0)),
    ("明天买材料 下午 3 点开会", "买材料 下午 3 点开会",
     date(2026, 11, 24), None),
    ("今天晚上 10 点半美股开盘后，提醒我卖出 SCHO", "美股开盘后，提醒我卖出 SCHO",
     TODAY, time(22, 30)),
    ("后天下午 3 点半开会，记得带电脑", "开会，记得带电脑",
     date(2026, 11, 25), time(15, 30)),
    ("明早 8 点交材料 10 点开会", "交材料 10 点开会",
     date(2026, 11, 24), time(8, 0)),
]


@pytest.mark.parametrize("text,title,due,at", INTERIOR_TIME)
def test_interior_time_does_not_win_the_clock(text, title, due, at):
    """句中（非句首前缀）的时间只留在标题里，不许抢 due/at。"""
    got = p(text)
    assert got.title == title
    assert got.due == due
    assert got.at == at


def test_prefix_date_only_falls_back_to_all_day_cutoff():
    """前缀只有日期、没有钟点 → 全天（at=None），不是正文钟点、也不再是 22:30。"""
    got = p("明天买材料 下午 3 点开会")
    assert got.at is None and got.all_day is True
    assert got.matched == "明天 下午 3 点"      # matched 仍回显全句时间原文，便于调试


def test_unresolvable_prefix_date_does_not_borrow_todays_cutoff():
    """日期解析不出来（13月45日）→ at 不许兜底成今天 22:30，念头该进收件箱。"""
    got = p("13月45日交材料")
    assert got.due is None and got.at is None


def test_prefix_date_plus_inner_clock_keeps_prefix_clock():
    """前缀有钟点、正文也有钟点 → 取前缀那个，正文那个只留在标题。"""
    got = p("明天中午前交材料，3 点开会")
    assert got.at == time(12, 0)
    assert got.title == "交材料，3 点开会"


# ---------- 69+：due 全天 + alarm 时刻（t_0541f6c3） ----------
#
# Reminder 的 due 与 alarm 是两件事：due 给全天（整天挂锁屏）、alarm 到点响一次。
# 「没钟点」不再是 22:30；上午 / 下午 只记时段标记；钟点仍然赢过时段词。

def test_date_only_is_all_day():
    """明天 买菜 → 全天（at=None、slot=None），不再是 22:30。"""
    got = p("明天 买菜")
    assert got.due == date(2026, 11, 24)
    assert got.at is None and got.slot is None
    assert got.all_day is True and got.title == "买菜"


def test_morning_slot_is_not_a_clock():
    """明天上午买菜 → slot=am（due 全天 + 12:00 响），不产具体时刻。"""
    got = p("明天上午买菜")
    assert got.due == date(2026, 11, 24)
    assert got.at is None and got.slot == "am"
    assert got.all_day is False


def test_afternoon_slot_is_not_a_clock():
    """明天下午买菜 → slot=pm（due 全天 + 18:00 响）。"""
    got = p("明天下午买菜")
    assert got.due == date(2026, 11, 24)
    assert got.at is None and got.slot == "pm"


def test_slot_without_date_is_not_all_day():
    """只说「下午」没说哪天 → 没有 due，就不是全天（CLI 落今天 + 18:00 响）。"""
    got = p("下午交材料")
    assert got.due is None and got.slot == "pm" and got.all_day is False


def test_noon_before_stays_a_precise_time():
    """中午前仍是精确 12:00（截止点），不退化成全天。"""
    got = p("明天中午前交材料")
    assert got.at == time(12, 0) and got.slot is None and got.all_day is False


def test_evening_clock_stays_precise():
    """今晚 22:30 卖出 → 精确 22:30（钟点赢过「晚上」这个时段词）。"""
    got = p("今晚 22:30 卖出")
    assert got.at == time(22, 30) and got.slot is None and got.all_day is False


def test_clock_beats_period_word():
    """明天下午 3 点开会 → 15:00 精确时刻，不许退成 slot=pm。"""
    got = p("明天下午 3 点开会")
    assert got.at == time(15, 0) and got.slot is None and got.all_day is False


# ---------- 重要性 → Reminders.app 原生 priority ----------
#
# 老板要的是「和 Reminder 里一致的重要级」，不是自造布尔标记：说「很重要 / 急」
# 就是 high，光说「重要 / 优先」是 medium。词可以在句中或句尾（时间词只剥句首），
# 但剥完不许把标题剥空、也不许把「优先股」「重要性」这种正文词剥掉。

def test_importance_high_words():
    assert p("对腾讯Q3财报做综合分析，这个很重要").priority == "high"
    assert p("明天买菜，急").priority == "high"
    assert p("着急：回老王电话").priority == "high"
    assert p("这件事非常重要").priority == "high"


def test_importance_medium_words():
    assert p("重要：整理投资笔记").priority == "medium"
    assert p("优先：把合同签了").priority == "medium"
    assert p("这个重要").priority == "medium"


def test_importance_no_words_is_none():
    assert p("明天下午交材料").priority == "none"
    assert Parsed(title="x").priority == "none"


def test_importance_word_at_tail_is_stripped():
    got = p("对腾讯Q3财报做综合分析，这个很重要")
    assert got.title == "对腾讯Q3财报做综合分析"
    assert "重要" not in got.title


def test_importance_word_at_head_is_stripped():
    got = p("很重要：交房租")
    assert got.title == "交房租" and got.priority == "high"


def test_importance_word_in_middle_is_stripped():
    """「很重要」在句中（后面跟标点）也要剥——时间词只剥句首，这个不受限。"""
    got = p("这个任务很重要，明天交")
    assert got.title == "这个任务，明天交"
    assert got.priority == "high"


def test_importance_only_sentence_keeps_title():
    """整句只剩「这个很重要」：标题不许变空，原句留着，优先级照标。"""
    got = p("这个很重要")
    assert got.title == "这个很重要" and got.priority == "high"


def test_importance_compound_words_are_not_touched():
    """正文里的「优先股 / 重要性 / 不着急 / 重要的事」不算重要性说法。"""
    cases = {
        "今天优先股持仓复盘": "优先股持仓复盘",
        "重要性分析报告": "重要性分析报告",
        "不着急，下周再说": "不着急，下周再说",
        "买鸡蛋，重要的事": "买鸡蛋，重要的事",
    }
    for text, expected in cases.items():
        got = p(text)
        assert got.priority == "none", text
        assert got.title == expected, text


