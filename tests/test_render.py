"""今日视图渲染测试（重写 4/6）。

只断言生成的 HTML 内容；只有一条真截图（Chrome 启动慢，别多开）。
"""

from __future__ import annotations

import re
from datetime import datetime, time

import pytest

from gtd.render import Entry, _find_chrome, _png_size, build_today_html, render_today

NOW = datetime(2026, 11, 23, 21, 40)          # 周一晚上 21:40


def entry(seq, title, *, at=None, list_name="GTD", overdue_days=0, kind="task"):
    return Entry(seq=seq, title=title, at=at, list_name=list_name,
                 overdue_days=overdue_days, kind=kind)


FIVE = [
    entry(1, "周会", at=time(9, 30), list_name="工作"),
    entry(2, "交材料", at=time(15, 0)),
    entry(3, "喝咖啡", at=time(18, 0), overdue_days=2),
    entry(4, "买鸡蛋", kind="inbox"),
    entry(5, "给车做保养", list_name="Waiting", kind="inbox"),
]


def visible(page: str) -> str:
    """去掉标签与 style/script，只留人眼能看到的字。"""
    page = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", page, flags=re.S)
    return re.sub(r"<[^>]+>", "", page)


def titles_of(page: str) -> list[str]:
    return re.findall(r'<div class="title">([^<]*)</div>', page)


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
    page, *_ = build_today_html([entry(1, "周会", at=time(9, 30))],
                                now=datetime(2026, 11, 23, 8, 0))
    assert "09:30" in visible(page)
    assert '<div class="at">09:30</div>' in page


def test_untimed_entry_leaves_clock_column_empty():
    page, *_ = build_today_html([entry(1, "有空再说")], now=NOW)
    assert '<div class="at"></div>' in page
    rows = page.split('<div class="foot">')[0]
    assert re.search(r"\d{2}:\d{2}", visible(rows)) is None, "无 at 不许凭空出现时刻"


# ---------- 4. 逾期标签 ----------

def test_overdue_renders_tag_in_red_brown():
    page, *_ = build_today_html([entry(3, "喝咖啡", overdue_days=2)], now=NOW)
    assert "逾期 2 天" in visible(page)
    assert '<span class="tag">逾期 2 天</span>' in page
    assert re.search(r"\.tag \{[^}]*#b4472e", page), "逾期标签必须是红褐色 #b4472e"
    assert '<div class="seq">3</div>' in page, "序号不许跟着变色"


def test_not_overdue_has_no_tag():
    page, *_ = build_today_html([entry(1, "正常事")], now=NOW)
    assert "逾期" not in visible(page).replace("逾期 0 件", "")


# ---------- 5. inbox 落下去 ----------

def test_inbox_entry_goes_to_lower_section():
    page, *_ = build_today_html(
        [entry(1, "周会", at=time(9, 30)), entry(2, "买鸡蛋", kind="inbox")], now=NOW)
    head, lower = page.split('class="lower"', 1)
    assert "买鸡蛋" in lower and "周会" not in lower
    assert "买鸡蛋" not in head, "inbox 条目不许留在上区"


def test_inbox_row_shows_no_clock():
    page, *_ = build_today_html([entry(2, "买鸡蛋", at=time(9, 30), kind="inbox")],
                                now=NOW)
    assert "09:30" not in page, "inbox 条目不显示时刻"


# ---------- 6. 序号不重排 ----------

def test_seq_uses_given_value_and_is_not_renumbered():
    entries = [entry(3, "甲"), entry(7, "乙"), entry(11, "丙")]
    page, *_ = build_today_html(entries, now=NOW)
    assert re.findall(r'<div class="seq">(\d+)</div>', page) == ["3", "7", "11"]


# ---------- 7. 顶栏三个数字 ----------

def test_top_bar_counts_match_entries():
    page, *_ = build_today_html(FIVE, now=NOW)
    text = visible(page)
    assert "今天 5 件" in text
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
    entries = [entry(1, "无时的"), entry(2, "晚的", at=time(18, 0)),
               entry(3, "早的", at=time(9, 30))]
    page, *_ = build_today_html(entries, now=NOW)
    assert titles_of(page) == ["早的", "晚的", "无时的"]


def test_past_clock_turns_red_brown():
    page, *_ = build_today_html([entry(1, "上午的事", at=time(9, 30))],
                                now=datetime(2026, 11, 23, 15, 0))
    assert '<div class="at past">09:30</div>' in page
    assert re.search(r"\.at\.past \{[^}]*#b4472e", page)


def test_future_clock_stays_grey():
    page, *_ = build_today_html([entry(1, "晚点的事", at=time(20, 0))],
                                now=datetime(2026, 11, 23, 9, 0))
    assert '<div class="at">20:00</div>' in page and "at past" not in page


def test_title_is_html_escaped():
    page, *_ = build_today_html([entry(1, '<b>看</b>盘')], now=NOW)
    assert "&lt;b&gt;看&lt;/b&gt;盘" in page and "<b>看</b>" not in page


def test_list_name_sits_in_right_column():
    page, *_ = build_today_html([entry(1, "给车做保养", list_name="Waiting")], now=NOW)
    assert '<div class="list">Waiting</div>' in page


def test_estimate_grows_with_rows():
    _p1, _w1, small = build_today_html([], now=NOW)
    _p2, _w2, big = build_today_html(FIVE, now=NOW)
    assert big > small


# ---------- 10. 真的出一张 PNG（唯一的截图用例） ----------

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
