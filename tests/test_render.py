"""今日视图渲染测试（重写 4/6）。

只断言生成的 HTML 内容；只有一条真截图（Chrome 启动慢，别多开）。

图上号（``Entry.number``）由命令层按 ``board_index`` 给：渲染层只印、不重排，
所以这里显式传号，断言「印出来的就是传进去的那个号」。``board_order()`` 另测。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta

import pytest

from gtd.render import (Entry, _find_chrome, _png_size, board_order, build_today_html,
                        render_today, render_today_json, render_today_markdown,
                        render_view, view_formats)

NOW = datetime(2026, 11, 23, 21, 40)          # 周一晚上 21:40


def entry(title, *, number=1, at=None, list_name="GTD", overdue_days=0, kind="task",
          due=None, ref="", priority="none"):
    return Entry(number=number, title=title, at=at, list_name=list_name,
                 overdue_days=overdue_days, kind=kind, due=due, ref=ref,
                 priority=priority)


def later(title, *, days, number=2, at=None, list_name="GTD", ref="", priority="none"):
    """未来第 days 天的条目（「接下来」区的候选）。"""
    return entry(title, number=number, at=at, list_name=list_name,
                 due=NOW.date() + timedelta(days=days), ref=ref, priority=priority)


def next_block(page: str) -> str:
    """只取「接下来」区那一段 HTML；整区没渲染时返回空串。"""
    if '<div class="next">' not in page:
        return ""
    tail = page.split('<div class="next">', 1)[1]
    return tail.split('<div class="lower"', 1)[0]


FIVE = [
    entry("周会", number=1, at=time(9, 30), list_name="工作"),
    entry("交材料", number=2, at=time(15, 0)),
    entry("喝咖啡", number=3, at=time(18, 0), overdue_days=2),
    entry("买鸡蛋", number=4, kind="inbox"),
    entry("给车做保养", number=5, list_name="Waiting", kind="inbox"),
]


def visible(page: str) -> str:
    """去掉标签与 style/script，只留人眼能看到的字。"""
    page = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", page, flags=re.S)
    return re.sub(r"<[^>]+>", "", page)


def titles_of(page: str) -> list[str]:
    return re.findall(r'<div class="title">([^<]*)</div>', page)


def numbers_of(page: str) -> list[str]:
    """图上所有号：今日/收件箱在 ``.seq``，接下来在 ``.when`` 里的 ``.seq``。"""
    return re.findall(r'<div class="(?:seq|when)">(?:<span class="seq">)?(\d+)', page)


# ---------- 1. 空列表 ----------

def test_empty_list_renders_top_and_footer_only():
    page, width, height = build_today_html([], now=NOW)
    assert width == 1080 and height > 0
    text = visible(page)
    assert "今天 0 件" in text and "逾期 0 件" in text and "收件箱 0 条" in text
    assert "只读 · 21:40" in text
    assert 'class="row"' not in page, "空列表不该有任何行"


# ---------- 2 / 3. 时刻列 ----------

def test_timed_entry_shows_clock():
    page, *_ = build_today_html([entry("周会", at=time(9, 30))],
                                now=datetime(2026, 11, 23, 8, 0))
    assert "09:30" in visible(page)
    assert '<div class="at">09:30</div>' in page


def test_untimed_entry_leaves_clock_column_empty():
    page, *_ = build_today_html([entry("有空再说")], now=NOW)
    assert '<div class="at"></div>' in page
    rows = page.split('<div class="foot">')[0]
    assert re.search(r"\d{2}:\d{2}", visible(rows)) is None, "无 at 不许凭空出现时刻"


# ---------- 4. 逾期标签 ----------

def test_overdue_renders_tag_in_red_brown():
    page, *_ = build_today_html([entry("喝咖啡", number=3, overdue_days=2)], now=NOW)
    assert "逾期 2 天" in visible(page)
    assert '<span class="tag">逾期 2 天</span>' in page
    assert re.search(r"\.tag \{[^}]*#b4472e", page), "逾期标签必须是红褐色 #b4472e"
    assert '<div class="seq">3</div>' in page, "序号不许跟着变色"


def test_not_overdue_has_no_tag():
    page, *_ = build_today_html([entry("正常事")], now=NOW)
    assert "逾期" not in visible(page).replace("逾期 0 件", "")


# ---------- 5. inbox 落下去 ----------

def test_inbox_entry_goes_to_lower_section():
    page, *_ = build_today_html(
        [entry("周会", number=1, at=time(9, 30)), entry("买鸡蛋", number=2, kind="inbox")],
        now=NOW)
    head, lower = page.split('class="lower"', 1)
    assert "买鸡蛋" in lower and "周会" not in lower
    assert "买鸡蛋" not in head, "inbox 条目不许留在上区"


def test_inbox_row_shows_no_clock():
    page, *_ = build_today_html([entry("买鸡蛋", at=time(9, 30), kind="inbox")], now=NOW)
    assert "09:30" not in page, "inbox 条目不显示时刻"


# ---------- 6. 号照传进来的印，不重排 ----------

def test_numbers_print_as_given_and_are_not_renumbered():
    """号是当天固定的（board_index），渲染层照印：3、7、11 不许变成 1、2、3。"""
    entries = [entry("甲", number=3), entry("乙", number=7), entry("乙二号", number=11)]
    page, *_ = build_today_html(entries, now=NOW)
    assert re.findall(r'<div class="seq">(\d+)</div>', page) == ["3", "7", "11"]


# ---------- 7. 顶栏三个数字 ----------

def test_top_bar_counts_match_entries():
    page, *_ = build_today_html(FIVE, now=NOW)
    text = visible(page)
    assert "今天 3 件" in text, "「今天」只数今日区那三行任务，收件箱行不算"
    assert "逾期 1 件" in text
    assert "收件箱 2 条" in text


# ---------- 8. 底部时间戳 ----------

def test_footer_stamp_is_read_only_and_clock():
    page, *_ = build_today_html(FIVE, now=NOW)
    assert re.search(r'<div class="foot">只读 · \d{2}:\d{2}</div>', page)
    assert "只读 · 21:40" in visible(page)


# ---------- 9. 不出现拟 UI 字样 ----------

def test_page_has_no_ui_furniture_words():
    page, *_ = build_today_html(FIVE, now=NOW)
    text = visible(page)
    for word in ("点击", "按钮", ">", "→", "▸", "查看"):
        assert word not in text, word


def test_page_has_no_table_or_card_decoration():
    page, *_ = build_today_html(FIVE, now=NOW)
    assert "<table" not in page and "<button" not in page
    assert "border:" not in page and "border-top" not in page, "不许有表格线/卡片边框"


# ---------- 排序、红褐色时刻、转义 ----------

def test_timed_sorted_ascending_untimed_last():
    entries = [entry("无时的"), entry("晚的", at=time(18, 0)), entry("早的", at=time(9, 30))]
    page, *_ = build_today_html(entries, now=NOW)
    assert titles_of(page) == ["早的", "晚的", "无时的"]


def test_past_clock_turns_red_brown():
    page, *_ = build_today_html([entry("上午的事", at=time(9, 30))],
                                now=datetime(2026, 11, 23, 15, 0))
    assert '<div class="at past">09:30</div>' in page
    assert re.search(r"\.at\.past \{[^}]*#b4472e", page)


def test_future_clock_stays_grey():
    page, *_ = build_today_html([entry("晚点的事", at=time(20, 0))],
                                now=datetime(2026, 11, 23, 9, 0))
    assert '<div class="at">20:00</div>' in page and "at past" not in page


def test_title_is_html_escaped():
    page, *_ = build_today_html([entry('<b>看</b>盘')], now=NOW)
    assert "&lt;b&gt;看&lt;/b&gt;盘" in page and "<b>看</b>" not in page


def test_list_name_sits_in_right_column():
    page, *_ = build_today_html([entry("给车做保养", list_name="Waiting")], now=NOW)
    assert '<div class="list">Waiting</div>' in page


def test_estimate_grows_with_rows():
    _p1, _w1, small = build_today_html([], now=NOW)
    _p2, _w2, big = build_today_html(FIVE, now=NOW)
    assert big > small


# ---------- 10. 「接下来」区（未来 7 天） ----------

def test_upcoming_within_seven_days_appears_with_weekday():
    page, *_ = build_today_html(
        [entry("今天的事", number=1, at=time(9, 30)),
         later("确认预约上门取车", number=2, days=4, at=time(15, 30)),
         later("把小米 SU7 送 4S 店保养", number=3, days=4, at=time(16, 0))],
        now=NOW)
    block = next_block(page)
    assert "确认预约上门取车" in block and "把小米 SU7 送 4S 店保养" in block
    assert "今天的事" not in block, "今日条目不许混进来"
    assert '<div class="when"><span class="seq">2</span>周五 15:30</div>' in page, \
        "日期前缀必须是星期几 + 时刻，号印在左边"


def test_upcoming_beyond_seven_days_is_hidden():
    page, *_ = build_today_html([later("下下周才做的事", days=8, at=time(10, 0))], now=NOW)
    assert "下下周才做的事" not in page
    assert "接下来" not in page, "没有未来条目时整区不渲染"


def test_upcoming_includes_the_seventh_day():
    page, *_ = build_today_html([later("第七天的事", days=7, at=time(10, 0))], now=NOW)
    assert "第七天的事" in next_block(page)
    assert "周一 10:00" in visible(next_block(page))


def test_upcoming_folds_after_five_with_count():
    entries = [later(f"第{i}件", number=i, days=i, at=time(9, 0)) for i in range(1, 8)]
    page, *_ = build_today_html(entries, now=NOW)
    block = next_block(page)
    assert len(re.findall(r'<div class="title">', block)) == 5, "最多露 5 条"
    assert '<div class="more">还有 2 条</div>' in page
    assert "还有 2 条" in visible(block)


def test_upcoming_section_absent_when_no_future_entries():
    page, *_ = build_today_html(FIVE, now=NOW)
    assert "接下来" not in page and 'class="next"' not in page


def test_upcoming_rows_keep_their_numbers():
    page, *_ = build_today_html(
        [entry("今天的事", number=1), later("周五的事", number=4, days=4, at=time(15, 30))],
        now=NOW)
    assert numbers_of(next_block(page)) == ["4"], "接下来区印当天固定的号（这里不许重排成 2）"


def test_upcoming_sorted_by_day_then_clock_untimed_last():
    entries = [later("周日无时", number=5, days=7),
               later("周三晚", number=4, days=2, at=time(18, 0)),
               later("周三早", number=3, days=2, at=time(9, 0)),
               later("周三无时", number=6, days=2),
               later("明天", number=2, days=1, at=time(8, 0))]
    page, *_ = build_today_html(entries, now=NOW)
    assert titles_of(next_block(page)) == ["明天", "周三早", "周三晚", "周三无时", "周日无时"]


def test_upcoming_shows_list_name_and_keeps_top_bar():
    page, *_ = build_today_html(
        [entry("今天的事", number=1, overdue_days=1),
         later("等对方回话", number=2, days=1, at=time(18, 0), list_name="Waiting")],
        now=NOW)
    assert '<div class="list">Waiting</div>' in next_block(page)
    text = visible(page)
    assert "今天 1 件" in text, "顶栏还是「今天 N 件」，接下来的不许算进去"
    assert "逾期 1 件" in text and "收件箱 0 条" in text


def test_upcoming_sits_between_today_rows_and_inbox():
    page, *_ = build_today_html(
        [entry("今天的事", number=1), later("周五的事", number=2, days=4),
         entry("买鸡蛋", number=3, kind="inbox")],
        now=NOW)
    assert page.index('class="rows"') < page.index("接下来") < page.index('class="lower"')
    assert "今天 1 件" in visible(page), "收件箱行不许再混进「今天」那一格（会重复计数）"


# ---------- 11. board_order：图上从上到下那一份（命令层照它挑号） ----------

def test_board_order_today_then_upcoming_then_inbox():
    entries = [entry("收件箱念头", number=1, kind="inbox"),
               later("周五的事", number=2, days=4, at=time(15, 30)),
               entry("晚的事", number=3, at=time(18, 0)),
               entry("早的事", number=4, at=time(9, 0))]
    assert [e.title for e in board_order(entries, now=NOW)] == \
        ["早的事", "晚的事", "周五的事", "收件箱念头"]


def test_board_order_leaves_out_what_is_not_drawn():
    """图上没画的（8 天后、第 6 条以后的未来条目）不许占号。"""
    entries = [entry("今天的事", number=1)]
    entries += [later(f"未来{i}", number=i, days=i, at=time(9, 0)) for i in range(1, 8)]
    entries.append(later("第八天", number=9, days=8, at=time(9, 0)))
    titles = [e.title for e in board_order(entries, now=NOW)]
    assert titles == ["今天的事", "未来1", "未来2", "未来3", "未来4", "未来5"]


# ---------- 11b. 全天 / 上午 / 下午（t_0541f6c3） ----------
#
# Reminder 的全天条目没有时刻：图上是「全天」；带 12:00 / 18:00 闹钟的写「上午 / 下午」。
# 全天不是逾期 —— 视觉上永不上红色。

def all_day(title, *, number=1, alarm=None, days=0):
    return Entry(number=number, title=title, at=None, list_name="GTD", overdue_days=0,
                 due=NOW.date() + timedelta(days=days), all_day=True, alarm=alarm)


def test_all_day_without_alarm_reads_all_day():
    page, *_ = build_today_html([all_day("买菜")], now=NOW)
    assert '<div class="at allday">全天</div>' in page


def test_all_day_is_never_red():
    """全天不是逾期：不许用 .past 的红褐色，另给更淡的灰 #8a8078。"""
    page, *_ = build_today_html([all_day("买菜")], now=NOW)
    assert "at past" not in page
    assert re.search(r"\.at\.allday \{[^}]*#8a8078", page)


def test_all_day_noon_alarm_reads_morning():
    page, *_ = build_today_html([all_day("买菜", alarm=time(12, 0))], now=NOW)
    assert '<div class="at allday">上午</div>' in page


def test_all_day_evening_alarm_reads_afternoon():
    page, *_ = build_today_html([all_day("取快递", alarm=time(18, 0))], now=NOW)
    assert '<div class="at allday">下午</div>' in page


def test_all_day_odd_alarm_keeps_the_clock():
    """非约定时刻（09:30）不硬套时段词 → 「全天 09:30」，不撒谎。"""
    page, *_ = build_today_html([all_day("体检", alarm=time(9, 30))], now=NOW)
    assert "全天 09:30" in page


def test_upcoming_all_day_shows_weekday_and_all_day():
    """接下来区：日期前缀照旧，时刻位置写「全天」。"""
    page, *_ = build_today_html(
        [entry("今天的事", at=time(9, 30)), all_day("买菜", number=2, days=2)], now=NOW)
    assert "周三 全天" in visible(page)


# ---------- 12. 真的出一张 PNG（唯一的截图用例） ----------

@pytest.mark.skipif(_find_chrome() is None, reason="本机没有 Chrome")
def test_render_today_writes_png(tmp_path):
    out = tmp_path / "today.png"
    path = render_today(FIVE, now=NOW, out_path=out)

    assert path == out and out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out.stat().st_size > 10 * 1024, out.stat().st_size
    width, height = _png_size(out)
    assert width == 2160, "宽 1080 @2x"
    assert height > 400
    assert list(tmp_path.glob("*.html")) == [], "临时 HTML 必须删掉"


# ---------- 13. 文字视图（Markdown / JSON，t_f85d8c13） ----------
#
# 与 board 图同一份分区、同一套号：编号由命令层给（Entry.number），这里只印不排。
# 空区整区不渲染；逾期在标题后加「（逾期 N 天）」；全天/上午/下午沿用图上同一个词。

def md(entries):
    return render_today_markdown(entries, now=NOW)


def md_rows(text: str) -> list[str]:
    """表格数据行（去掉表头与分隔行）。"""
    return [line for line in text.splitlines()
            if line.startswith("| ") and not line.startswith("| # |")]


def test_markdown_today_row_has_number_clock_title_list():
    text = md([entry("卖出 ≥550 股 SCHO", number=1, at=time(22, 30))])
    assert "**今天 1 件 · 逾期 0 件 · 收件箱 0 条**" in text
    assert "| # | 时刻 | 标题 | 列表 |" in text
    assert "| 1 | 22:30 | 卖出 ≥550 股 SCHO | GTD |" in text


def test_markdown_next_section_carries_weekday_and_clock():
    text = md([later("把小米 SU7 送 4S 店保养", days=4, number=3, at=time(16, 0))])
    assert "**接下来**" in text
    assert "| # | 日期时刻 | 标题 | 列表 |" in text
    assert "| 3 | 周五 16:00 | 把小米 SU7 送 4S 店保养 | GTD |" in text


def test_markdown_all_day_morning_afternoon_words():
    text = md([all_day("买菜", number=1),
               all_day("取快递", number=2, alarm=time(12, 0)),
               all_day("开会", number=3, alarm=time(18, 0))])
    clocks = [row.split(" | ")[1] for row in md_rows(text)]
    assert clocks == ["全天", "上午", "下午"], clocks


def test_markdown_overdue_appends_days_to_title():
    text = md([entry("昨天该做完的", number=2, at=time(18, 0), overdue_days=2)])
    assert "| 2 | 18:00 | 昨天该做完的（逾期 2 天） | GTD |" in text


def test_markdown_empty_sections_are_not_rendered():
    """只有收件箱一条：不出现「接下来」标题、不出现时刻表；今天表也不渲染。"""
    text = md([entry("想到一个点子", number=1, kind="inbox")])
    assert "**接下来**" not in text
    assert "| # | 时刻 | 标题 | 列表 |" not in text
    assert "**收件箱**" in text
    assert "| 1 | 想到一个点子 |" in text


def test_markdown_empty_store_shows_summary_only():
    assert md([]) == "**今天 0 件 · 逾期 0 件 · 收件箱 0 条**\n"



def test_markdown_numbers_print_as_given_and_are_not_renumbered():
    """图上号是 board_index 给的：今天 4 号、接下来 9 号，原样印出来。"""
    text = md([entry("甲", number=4, at=time(9, 0)),
               later("乙", days=1, number=9, at=time(10, 0))])
    assert [row.split(" | ")[0] for row in md_rows(text)] == ["| 4", "| 9"]


def test_markdown_next_folds_after_five_with_count():
    rows = [later(f"第{day}天", days=day, number=day + 1, at=time(9, 0)) for day in range(1, 7)]
    text = md(rows)
    assert len(md_rows(text)) == 5
    assert "还有 1 条" in text


def test_markdown_escapes_pipes_in_titles():
    text = md([entry("A | B", number=1, at=time(9, 0))])
    assert r"A \| B" in text and r"| 1 | 09:00 | A \| B | GTD |" in text


def test_json_view_is_structured_with_numbers():
    text = render_today_json([entry("甲", number=1, at=time(22, 30)),
                              entry("想到一个点子", number=2, kind="inbox")], now=NOW)
    data = json.loads(text)
    # 「今天」= 今日区行数：收件箱单独一格，不混进来（否则顶栏比数组多 1）
    assert data["summary"] == {"today": 1, "overdue": 0, "inbox": 1}
    assert len(data["today"]) == data["summary"]["today"]
    assert [item["number"] for item in data["today"]] == [1]
    assert data["today"][0]["when"] == "22:30"
    assert [item["title"] for item in data["inbox"]] == ["想到一个点子"]


def test_today_count_equals_visible_today_rows():
    """锁死口径：图上「今天 N 件」== 今日区行数，文字视图同数（收件箱/未来都不许混入）。"""
    entries = [entry("今天的甲", number=1, at=time(9, 0)),
               entry("昨天的乙", number=2, at=time(8, 0), overdue_days=1),
               later("周五的丙", number=3, days=4, at=time(16, 0)),
               entry("买鸡蛋", number=4, kind="inbox"),
               entry("给车做保养", number=5, kind="inbox")]
    page, *_ = build_today_html(entries, now=NOW)
    assert "今天 2 件" in visible(page)
    rows = page.split('class="rows"', 1)[1].split('class="next"', 1)[0]
    assert re.findall(r'<div class="seq">(\d+)</div>', rows) == ["2", "1"], \
        "今日区按时刻升序排（08:00 的逾期条在前），号是 board_index 给的、不参与排序"
    data = json.loads(render_today_json(entries, now=NOW))
    assert data["summary"]["today"] == len(data["today"]) == 2


def test_view_formats_are_md_and_json():
    assert view_formats() == ("json", "md")
    assert render_view([], now=NOW) == render_today_markdown([], now=NOW)
    assert json.loads(render_view([], now=NOW, fmt="json"))["summary"] == {
        "today": 0, "overdue": 0, "inbox": 0}


def test_render_view_rejects_unknown_format():
    with pytest.raises(ValueError, match="未知格式"):
        render_view([], now=NOW, fmt="xml")


# ---------- 12. 优先级：感叹号标记 + 收件箱按优先级排 ----------
#
# 口径与 Reminders.app 一致：high !!!、medium !!、low !、none 不印。
# 只影响收件箱区的顺序；今天区 / 接下来区仍按日期排（它们也印标记，但顺序不动）。

def test_inbox_rows_sort_by_priority_high_first():
    entries = [entry("买鸡蛋", number=4, kind="inbox"),
               entry("整理财报", number=5, kind="inbox", priority="medium"),
               entry("交房租", number=6, kind="inbox", priority="high")]
    md = render_today_markdown(entries, now=NOW)
    assert (md.index("| 6 | !!! 交房租 |")
            < md.index("| 5 | !! 整理财报 |")
            < md.index("| 4 | 买鸡蛋 |"))


def test_priority_marks_show_up_in_png_html():
    page, *_ = build_today_html(
        [entry("交房租", number=3, kind="inbox", priority="high")], now=NOW)
    assert "!!! 交房租" in visible(page)


def test_task_priority_mark_shows_in_today_and_next_sections():
    md = render_today_markdown([entry("今天交材料", number=1, priority="high"),
                                later("周五的事", number=2, days=4, priority="medium")],
                               now=NOW)
    assert "!!! 今天交材料" in md and "!! 周五的事" in md


def test_view_json_reports_priority():
    data = json.loads(render_today_json(
        [entry("交房租", number=4, kind="inbox", priority="high")], now=NOW))
    assert data["inbox"][0]["priority"] == "high"


def test_board_order_puts_important_inbox_thoughts_first():
    entries = [entry("普通念头", number=4, kind="inbox"),
               entry("重要念头", number=5, kind="inbox", priority="high")]
    assert [e.title for e in board_order(entries, now=NOW)][-2:] == ["重要念头", "普通念头"]
