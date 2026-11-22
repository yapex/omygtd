"""阶段 2 · 渲染层测试（先写测试，后写实现）。

渲染层只吃「统一条目流」，不认识 SQLite —— 这里所有条目都是手写的。
"""

from __future__ import annotations

import json
import re

import pytest

from gtd.render import (
    Artifact,
    JsonRenderer,
    MdRenderer,
    PngRenderer,
    build_html,
    fold_line,
    get_renderer,
    register_renderer,
    select_view,
    when_label,
)

DAY = "2026-11-22"
ORDER = ("购物", "投资", "家庭", "健康", "家务", "学习", "工作", "预约", "其他")


def visible(page: str) -> str:
    """去掉标签，只留可见文字（折叠行逐字比对用）。"""
    return re.sub(r"<[^>]+>", "", page)


def entry(seq, title, *, category="其他", slot="all", due=DAY, status="next",
          tags=(), id=None):
    return {
        "kind": "task", "id": id if id is not None else 100 + seq, "seq": seq,
        "title": title, "slot": slot, "category": category, "tags": list(tags),
        "due": due, "defer": None, "status": status, "waiting_on": None, "note": None,
        "capture_id": f"cap-{seq}", "completed_at": None,
        "created_at": "2026-11-22T09:00:00+08:00",
        "updated_at": "2026-11-22T09:00:00+08:00", "sync_state": "none",
    }


@pytest.fixture()
def mixed_stream():
    """1 条今天到期的 + 4 条投资（下一步、未来）+ 3 条收件箱。"""
    return [
        entry(1, "打款给房东", category="预约", slot="am", due=DAY),
        entry(2, "看盘", category="投资", due="2026-11-30"),
        entry(3, "读财报", category="投资", due="2026-11-30"),
        entry(4, "调仓", category="投资", due="2026-11-30"),
        entry(5, "算估值", category="投资", due="2026-11-30"),
        entry(6, "买鸡蛋", due=None, status="inbox"),
        entry(7, "预约体检", due=None, status="inbox"),
        entry(8, "剪头发", due=None, status="inbox"),
    ]


# ---------- 折叠文案（逐字） ----------

def test_fold_line_text_exact():
    assert fold_line("投资", 4, 2) == "投资共 4 条，这里 2 条 · 说「看投资」给全部"


def test_fold_line_inbox_text_exact():
    assert fold_line("收件箱", 5, 2) == "收件箱共 5 条，这里 2 条 · 说「看收件箱」给全部"


# ---------- 视图分区 ----------

def test_select_view_today_uses_due_only(mixed_stream):
    today = select_view(mixed_stream, "today", DAY)
    assert [e["title"] for e in today] == ["打款给房东"]


def test_select_view_inbox(mixed_stream):
    assert len(select_view(mixed_stream, "inbox", DAY)) == 3


def test_select_view_skips_done(mixed_stream):
    mixed_stream.append(entry(9, "已完成的", status="done", due=DAY))
    assert all(e["status"] != "done" for e in select_view(mixed_stream, "all", DAY))


# ---------- md：不折叠 ----------

def test_md_not_folded(mixed_stream):
    art = MdRenderer().render(mixed_stream, "all", {"day": DAY})
    rows = [line for line in art.text.splitlines()
            if line.startswith("| ") and not line.startswith("| # ")]
    assert len(rows) == 8, "md 必须全量，不许折叠"
    assert "这里 2 条" not in art.text


def test_md_has_seq_and_id_columns(mixed_stream):
    text = MdRenderer().render(mixed_stream, "today", {"day": DAY}).text
    assert "| # | id |" in text
    assert "| 1 | 101 |" in text


# ---------- json：完整字段 ----------

def test_json_has_all_fields(mixed_stream):
    art = JsonRenderer().render(mixed_stream, "all", {"day": DAY})
    payload = json.loads(art.text)
    assert payload["count"] == 8
    assert payload["view"] == "all"
    first = payload["entries"][0]
    for key in ("kind", "id", "seq", "title", "slot", "category", "tags",
                "due", "status", "waiting_on", "note", "created_at"):
        assert key in first


# ---------- png：折叠生效 ----------

def test_today_png_folds_each_category_to_two(mixed_stream):
    html, _w, _h = build_html(mixed_stream, "today", {"day": DAY})
    text = visible(html)
    assert "投资共 4 条，这里 2 条 · 说「看投资」给全部" in text
    assert "收件箱共 3 条，这里 2 条 · 说「看收件箱」给全部" in text
    assert html.count("看盘") == 1 and html.count("读财报") == 1
    assert "调仓" not in html and "算估值" not in html


def test_png_no_fold_when_within_limit():
    stream = [entry(1, "甲", category="投资"), entry(2, "乙", category="投资")]
    html, _w, _h = build_html(stream, "today", {"day": DAY})
    assert "共 2 条" not in visible(html)


def test_today_section_never_folds(mixed_stream):
    """今日区不折叠：4 条到期条目必须全部出现，且不产生无分类名的折叠行。"""
    stream = [entry(i, f"今日事{i}") for i in range(1, 5)]
    html, _w, _h = build_html(stream, "today", {"day": DAY})
    text = visible(html)
    assert "这里 2 条" not in text
    for i in range(1, 5):
        assert f"今日事{i}" in text


def test_inbox_png_lists_everything_numbered(mixed_stream):
    """收件箱视图折叠等于自毁功能：必须全量、带 1..N 编号、顶部没有三格。"""
    html, _w, _h = build_html(mixed_stream, "inbox", {"day": DAY})
    text = visible(html)
    assert 'class="stats"' not in html, "收件箱视图不要今天的三格"
    assert "收件箱共 3 条，这里 2 条" not in text, "收件箱视图不许折叠"
    for title in ("买鸡蛋", "预约体检", "剪头发"):
        assert title in html, title
    numbers = re.findall(r'<div class="seq">(\d+)</div>', html)
    assert numbers == ["1", "2", "3"], numbers


def test_inbox_png_intro_is_one_line_and_counts_oldest():
    stream = [entry(1, "新记的", status="inbox", due=None),
              entry(2, "放久的", status="inbox", due=None)]
    stream[1]["created_at"] = "2026-11-17T09:00:00+08:00"      # 落后 5 天
    html, _w, _h = build_html(stream, "inbox", {"day": DAY})
    text = visible(html)
    assert "2 条待澄清 · 最老 5 天" in text
    assert "说「1 归到购物」「2、3 删掉」就能处理。" in text
    assert ">逾期<" not in text and "今日完成" not in text


def test_inbox_age_tag_always_shown_and_red_after_three_days():
    stream = [entry(1, "新记的", status="inbox", due=None),
              entry(2, "放三天的", status="inbox", due=None),
              entry(3, "放四天的", status="inbox", due=None)]
    stream[1]["created_at"] = "2026-11-19T09:00:00+08:00"      # 3 天，还不红
    stream[2]["created_at"] = "2026-11-18T09:00:00+08:00"      # 4 天，超过 3 天要红
    html, _w, _h = build_html(stream, "inbox", {"day": DAY})
    assert '<span class="tag">0 天</span>' in html
    assert '<span class="tag">3 天</span>' in html
    assert '<span class="tag late">4 天</span>' in html


def test_categories_png_is_batch_triage_view(mixed_stream):
    html, _w, _h = build_html(mixed_stream, "categories",
                              {"day": DAY, "category_order": ORDER})
    text = visible(html)
    assert "GTD 分类管理" in text
    assert "条还没归类" in text
    assert "说「1、5、7 归到购物」就能批量归。归错了说「3 改到家庭」。" in text
    assert 'class="stats"' not in html, "三格是 today 视图的东西，分类管理不要"
    assert "这里 2 条" not in text, "给批量操作用的视图不许折叠"


def test_categories_header_shows_date_and_time(mixed_stream):
    """定稿表头：GTD 分类管理 + 11月22日 周日 · 18:50（时间不能丢）。"""
    html, _w, _h = build_html(mixed_stream, "categories",
                             {"day": DAY, "category_order": ORDER,
                              "generated_at": "2026-11-22T18:50:00+08:00"})
    assert "GTD 分类管理" in visible(html)
    assert "11月22日 周日 · 18:50" in visible(html)


def test_categories_numbering_is_continuous(mixed_stream):
    html, _w, _h = build_html(mixed_stream, "categories",
                              {"day": DAY, "category_order": ORDER})
    numbers = re.findall(r'<div class="seq">(\d+)</div>', html)
    assert numbers == [str(i) for i in range(1, 9)], numbers
    text = visible(html)
    assert text.index("其他") < text.index("预约") < text.index("投资")


def test_categories_other_is_boxed_first_and_red(mixed_stream):
    html, _w, _h = build_html(mixed_stream, "categories",
                              {"day": DAY, "category_order": ORDER})
    assert 'class="boxed"' in html
    assert '<span class="count bad">3</span>' in html, "其他 的数量要红色"
    assert html.index('class="boxed"') < html.index(">预约"), "其他 必须排最前"


def test_categories_without_other_items_has_no_box():
    stream = [entry(1, "买鸡蛋", category="购物")]
    html, _w, _h = build_html(stream, "categories", {"day": DAY, "category_order": ORDER})
    assert 'class="boxed"' not in html
    assert "全部已归类" in visible(html)


def test_categories_header_and_footer_time():
    """分类管理不显示时间在表头，但页脚必须有生成时刻，且表头不许留孤零零的分隔点。"""
    ctx = {"day": DAY, "generated_at": "2026-11-22T18:50:00+08:00", "category_order": ORDER}
    html, _w, _h = build_html([], "categories", ctx)
    assert "周日 · </div>" not in html
    assert "生成时刻 18:50" in html


# ---------- 定稿版式 ----------

def test_png_uses_final_layout(mixed_stream):
    html, width, _h = build_html(mixed_stream, "today", {"day": DAY})
    assert width == 1080
    assert "#f7f5f0" in html, "米白底"
    assert "GTD 每日看板" in html
    assert "今日 · 含逾期" not in html, "定稿没有这个大标题"
    assert "color: #c3b9ae" in html, "序号灰色"


def test_png_stats_order_is_overdue_done_inbox(mixed_stream):
    html, _w, _h = build_html(mixed_stream, "today", {"day": DAY})
    assert html.index(">逾期</span>") < html.index(">今日完成</span>") < html.index("收件箱 · 最老")


def test_png_stats_numbers_match_stream(mixed_stream):
    mixed_stream[1]["due"] = "2026-11-01"          # 投资一
    mixed_stream[5]["status"] = "inbox"            # 已经 inbox
    html, _w, _h = build_html(mixed_stream, "today", {"day": DAY})
    assert '<div class="stat bad"><b>1</b><span>逾期</span>' in html
    assert '<div class="stat"><b>0</b><span>今日完成</span>' in html
    assert "<b>3</b><span>收件箱 · 最老" in html


def test_defer_only_entry_shows_start_date():
    item = entry(9, "学 Rust", due=None)
    item["defer"] = "2026-12-15"
    assert when_label(item, DAY) == "12/15 起"


def test_png_renderer_writes_file(tmp_path, mixed_stream):
    out = tmp_path / "today.png"
    art = PngRenderer().render(mixed_stream, "today", {"day": DAY, "out": out})
    assert art.path == out and out.exists() and out.stat().st_size > 5000
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- 策略注册表（开闭原则） ----------

def test_registry_has_three_formats():
    assert isinstance(get_renderer("json"), JsonRenderer)
    assert isinstance(get_renderer("md"), MdRenderer)
    assert isinstance(get_renderer("png"), PngRenderer)


def test_new_format_needs_only_a_new_class():
    class CsvRenderer:
        def render(self, stream, view, ctx):
            return Artifact(text="a,b\n", mime="text/csv")

    register_renderer("csv", CsvRenderer)
    art = get_renderer("csv").render([], "today", {"day": DAY})
    assert art.text == "a,b\n"
    assert isinstance(get_renderer("md"), MdRenderer)


def test_renderer_artifacts_are_uniform(mixed_stream):
    for fmt in ("json", "md"):
        art = get_renderer(fmt).render(mixed_stream, "today", {"day": DAY})
        assert isinstance(art, Artifact) and art.text and art.path is None
