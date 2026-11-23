"""渲染层：今日视图（一张图）+ 遗留的 json / md / png 注册表。

职责边界（S/I）：只认条目结构，不查库、不 import sqlite3、不认识命令参数。

v2 的今日视图在文件末尾「v2 今日视图（一张图）」一节：输入是 ``Entry`` 条目流，
输出一张 PNG（``render_today``）。不查库、不调 remindctl，纯输入输出。

文件前半部分是 v1 遗留的策略注册表（``get_renderer('json'|'md'|'png')``）——
``cli.py`` 还在用，5/6 卡接线才删；删之前两套并存，互不调用。
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Protocol

from . import priority as prio
from . import repeat as rep

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
FONT_STACK = '-apple-system, "PingFang SC", "Helvetica Neue", sans-serif'
VIEW_TITLES = {"today": "今日", "inbox": "收件箱", "categories": "分类", "all": "全部"}
STATUS_CN = {"inbox": "收件箱", "next": "下一步", "done": "已完成", "dropped": "已放弃"}
VIEWS = tuple(VIEW_TITLES)
FOLD_LIMIT = 2

PAGE_W = 1080
PAD_TOP, PAD_BOTTOM, PAD_X = 56, 40, 56
LINE_H = 47
ROW_PAD = 24
PROJ_H = 48
MORE_H = 43
DONE_H = 63
SECTION_H = 84
TOP_H = 34
STATS_H = 176
FOOT_H = 92
TEXT_W = PAGE_W - PAD_X * 2 - 58
TAG_EXTRA_H = 4
SAFETY_H = 14
INTRO_H = 158
BOX_EXTRA_H = 30
LIST_GAP_H = 42


# ---------------------------------------------------------------- 接口

@dataclass(frozen=True)
class Artifact:
    """渲染产物：要么是文本（md/json），要么是文件（png）。"""

    text: str | None = None
    path: Path | None = None
    mime: str = "text/plain; charset=utf-8"


class Renderer(Protocol):
    def render(self, stream: list[dict], view: str, ctx: dict) -> Artifact:
        ...


_REGISTRY: dict[str, Renderer] = {}


def register_renderer(name: str, renderer_cls) -> None:
    _REGISTRY[name] = renderer_cls()


def get_renderer(name: str) -> Renderer:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"未知格式：{name}（可选：{'/'.join(formats())}）") from exc


def formats() -> tuple[str, ...]:
    """已注册格式名（命令层据此给出 choices，不写死分支）。"""
    return tuple(sorted(_REGISTRY))


# ---------------------------------------------------------------- 条目流工具

def is_open(entry: dict) -> bool:
    return entry.get("status") in ("inbox", "next")


def select_view(stream: list[dict], view: str, day: str) -> list[dict]:
    """按视图挑条目。today 的口径与验收 SQL 完全一致：未完成且 due <= 今天。"""
    if view == "today":
        return [e for e in stream if is_open(e) and (e.get("due") or "") and e["due"] <= day]
    if view == "inbox":
        return [e for e in stream if e.get("status") == "inbox"]
    if view in ("all", "categories"):
        return [e for e in stream if is_open(e)]
    raise ValueError(f"未知视图：{view}（可选：{'/'.join(VIEWS)}）")


def fold_line(label: str, total: int, shown: int) -> str:
    return f"{label}共 {total} 条，这里 {shown} 条 · 说「看{label}」给全部"


def fold(entries: list[dict], limit: int = FOLD_LIMIT, label: str = ""):
    """折叠：返回 (展示的前 N 条, 折叠行文案或 None)。label 为空不折叠。"""
    if not label or len(entries) <= limit:
        return list(entries), None
    return list(entries[:limit]), fold_line(label, len(entries), limit)


def _day_delta(due: str, day: str) -> int | None:
    try:
        return (date.fromisoformat(due[:10]) - date.fromisoformat(day)).days
    except ValueError:
        return None


def _short_date(due: str, day: str) -> str:
    if due[:4] == day[:4]:
        return f"{int(due[5:7])}/{int(due[8:10])}"
    return due


def when_label(entry: dict, day: str) -> str:
    """§八 时间显示规则；只有 defer 的条目显示「12/15 起」。"""
    due, slot = entry.get("due") or "", entry.get("slot") or "all"
    if not due:
        defer = entry.get("defer") or ""
        return f"{_short_date(defer, day)} 起" if defer else "无期限"
    delta = _day_delta(due, day)
    prefix = {0: "", 1: "明天", 2: "后天", -1: "昨天", -2: "前天"}.get(delta)
    if prefix is None:
        prefix = _short_date(due, day)
    part = {"all": "", "am": "上午", "pm": "下午"}.get(slot)
    if part is None:
        kind, hhmm = slot.split(":", 1)
        part = f"{hhmm} 前" if kind == "before" else hhmm
    if prefix and part:
        return f"{prefix} {part}"
    return prefix or part or "今天"


def repeat_label(entry: dict) -> str:
    """周期标签「每 N 天」；一次性任务返回空串（条目流里可能没有这个键）。"""
    days = entry.get("repeat_days")
    return f"每 {days} 天" if days else ""


def _by_category(entries: list[dict]) -> list[tuple[str, list[dict]]]:
    order, buckets = [], {}
    for entry in entries:
        name = entry.get("category") or "其他"
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        buckets[name].append(entry)
    return [(name, buckets[name]) for name in order]


def _age_days(entry: dict, day: str) -> int:
    try:
        created = date.fromisoformat((entry.get("created_at") or "")[:10])
        return max(0, (date.fromisoformat(day) - created).days)
    except ValueError:
        return 0


# ---------------------------------------------------------------- json

class JsonRenderer:
    """完整字段，程序对接用。"""

    def render(self, stream, view, ctx) -> Artifact:
        day = ctx["day"]
        entries = select_view(stream, view, day)
        payload = {"ok": True, "view": view, "day": day, "count": len(entries),
                   "generated_at": ctx.get("generated_at"), "entries": entries}
        if ctx.get("stats"):
            payload["stats"] = ctx["stats"]
        return Artifact(text=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        mime="application/json")


# ---------------------------------------------------------------- md

class MdRenderer:
    """表格，含 # 与 id 两列；不折叠、全量。"""

    def render(self, stream, view, ctx) -> Artifact:
        day = ctx["day"]
        entries = select_view(stream, view, day)
        lines = [f"## {VIEW_TITLES[view]} · {day}（{len(entries)} 条）", "",
                 "| # | id | 任务 | 分类 | 时间 | 周期 | 状态 |",
                 "|---|---|---|---|---|---|---|"]
        lines += [self._row(entry, day) for entry in entries]
        return Artifact(text="\n".join(lines) + "\n", mime="text/markdown; charset=utf-8")

    @staticmethod
    def _row(entry: dict, day: str) -> str:
        seq = entry.get("seq") if entry.get("seq") is not None else "-"
        cells = [str(seq), str(entry.get("id")), entry.get("title") or "",
                 entry.get("category") or "", when_label(entry, day),
                 repeat_label(entry), STATUS_CN.get(entry.get("status"), "")]
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


# ---------------------------------------------------------------- png

class PngRenderer:
    """Chrome headless 截图；版式照定稿 HTML（米白底、细分割线、灰色序号、顶部三格）。"""

    def render(self, stream, view, ctx) -> Artifact:
        page, width, height = build_html(stream, view, ctx)
        out = Path(ctx.get("out") or f"{view}-{ctx['day']}.png").expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        _screenshot(page, out, width, height)
        return Artifact(path=out, mime="image/png")


def build_html(stream: list[dict], view: str, ctx: dict):
    """返回 (html, 宽, 高)。每个视图一套 HTML 骨架，高度按行数精确推算。"""
    return PAGE_BUILDERS[view](stream, view, ctx)


def _today_page(stream, view, ctx):
    """today / all：顶部三格（逾期 / 今日完成 / 收件箱）。"""
    day = ctx["day"]
    blocks, blocks_h = _blocks(stream, view, day, ctx)
    top_html, top_h = _stats_html(_stats(stream, day)), STATS_H
    height = PAD_TOP + TOP_H + top_h + blocks_h + FOOT_H + PAD_BOTTOM + SAFETY_H
    return _page(blocks, day, ctx, height, top_html), PAGE_W, height


def _inbox_page(stream, view, ctx):
    """inbox：一行「N 条待澄清 · 最老 N 天」+ 例句，全量、编号、每条带天数。"""
    day = ctx["day"]
    items = select_view(stream, view, day)
    inner, inner_h = _inbox_rows(items, day)
    height = (PAD_TOP + TOP_H + INTRO_H + LIST_GAP_H + inner_h
              + FOOT_H + PAD_BOTTOM + SAFETY_H)
    blocks = f'<section class="inbox-list">{inner}</section>'
    page = _page(blocks, day, ctx, height, _inbox_intro_html(items, day), brand="GTD 收件箱")
    return page, PAGE_W, height


def _categories_page(stream, view, ctx):
    """categories：顶部「N 条还没归类」+ 例句，「其他」框在最前，编号跨分类连续。"""
    day = ctx["day"]
    blocks, blocks_h = _categories_blocks(stream, day, ctx)
    top_html, top_h = _intro_html(_uncategorized_count(stream)), INTRO_H
    height = PAD_TOP + TOP_H + top_h + blocks_h + FOOT_H + PAD_BOTTOM + SAFETY_H
    page = _page(blocks, day, ctx, height, top_html, brand="GTD 分类管理")
    return page, PAGE_W, height


PAGE_BUILDERS = {"today": _today_page, "all": _today_page,
                 "inbox": _inbox_page, "categories": _categories_page}


def _stats_html(stats) -> str:
    overdue, done_today, inbox_n, oldest = stats
    return (f'<div class="stats"><div class="stat bad"><b>{overdue}</b><span>逾期</span></div>'
            f'<div class="stat"><b>{done_today}</b><span>今日完成</span></div>'
            f'<div class="stat"><b>{inbox_n}</b><span>收件箱 · 最老 {oldest} 天</span></div></div>')


def _uncategorized_count(stream) -> int:
    return sum(1 for e in stream
               if is_open(e) and (e.get("category") or "其他") == "其他")


def _intro_html(blind_n: int) -> str:
    """分类管理视图的顶部：待归类条数 + 批量归类例句（语法必须写出来，否则用户靠猜）。"""
    head = "全部已归类" if blind_n == 0 else f"<b>{blind_n}</b>条还没归类"
    return ('<div class="intro"><div class="blind">' + head + '</div>'
            '<div class="hint">说「1、5、7 归到购物」就能批量归。归错了说「3 改到家庭」。</div></div>')


def _inbox_intro_html(items: list[dict], day: str) -> str:
    """收件箱视图的顶部：一行「N 条待澄清 · 最老 N 天」+ 澄清例句。"""
    if not items:
        return '<div class="intro"><div class="blind">收件箱空了</div></div>'
    oldest = max(_age_days(entry, day) for entry in items)
    head = f"<b>{len(items)}</b> 条待澄清 · 最老 {oldest} 天"
    return ('<div class="intro"><div class="blind">' + head + '</div>'
            '<div class="hint">说「1 归到购物」「2、3 删掉」就能处理。</div></div>')


def _inbox_rows(items: list[dict], day: str):
    """收件箱全量列出：1..N 连续编号 + 每条右侧「记了几天」，绝不折叠。"""
    if not items:
        return '<div class="empty">— 收件箱是空的 —</div>', 52
    parts, height = [], 0
    for number, entry in enumerate(items, start=1):
        row, row_h = _inbox_row(entry, day, number)
        parts.append(row)
        height += row_h
    return "".join(parts), height


def _inbox_row(entry: dict, day: str, number: int):
    """红标签判据：记了超过 3 天（>3），0 天也照样显示，方便判断优先级。"""
    age = _age_days(entry, day)
    return _row_with(entry, f"{age} 天", "late" if age > 3 else "", True, number)


def _blocks(stream, view, day, ctx):
    if view == "today":
        return _today_blocks(stream, day, ctx)
    rows = select_view(stream, "all", day)
    return _section_rows("全部", rows, day, show_seq=True)



# ---- 各区拼接 ----

def _today_blocks(stream, day, ctx):
    open_due = select_view(stream, "today", day)
    overdue = [e for e in open_due if e["due"] < day]
    today = [e for e in open_due if e["due"] == day]
    done = [e for e in stream if e["status"] == "done"
            and (e.get("completed_at") or "")[:10] == day]
    used = {e["id"] for e in open_due}
    rest = [e for e in stream if is_open(e) and e["id"] not in used]
    parts, height = [], 0
    if today or done:
        inner, inner_h = _rows(today, day, ctx, show_seq=True)
        if done:
            inner += _done_note(done)
            inner_h += DONE_H
        block, block_h = _section("今日", inner, inner_h)
        parts.append(block)
        height += block_h
    if overdue:
        block, block_h = _section_rows("逾期", overdue, day, ctx=ctx, show_seq=True)
        parts.append(block)
        height += block_h
    next_html, next_h = _next_groups([e for e in rest if e["status"] == "next"], day, ctx)
    parts.append(next_html)
    height += next_h
    inbox_html, inbox_h = _inbox_block(stream, day)
    return "".join(parts) + inbox_html, height + inbox_h


def _categories_blocks(stream, day, ctx):
    """分类管理：编号跨分类连续，「其他」区排最前并框出来，绝不折叠。"""
    entries = select_view(stream, "categories", day)
    parts, height, counter = [], 0, 0
    for name, items in _ordered_groups(entries, ctx.get("category_order") or ()):
        inner, inner_h, counter = _numbered_rows(items, day, counter)
        badge = ('<span class="count bad">' if name == "其他" else '<span class="count">')
        title = f'<h2>{html.escape(name)}{badge}{len(items)}</span></h2>'
        if name == "其他":
            parts.append(f'<section class="boxed">{title}{inner}</section>')
            height += SECTION_H + BOX_EXTRA_H + inner_h
        else:
            parts.append(f"<section>{title}{inner}</section>")
            height += SECTION_H + inner_h
    return "".join(parts), height


def _ordered_groups(entries, order):
    """「其他」第一，其余按词表顺序，词表外的按出现顺序垫后。"""
    buckets = {}
    for entry in entries:
        buckets.setdefault(entry.get("category") or "其他", []).append(entry)
    ordered = [name for name in order if name in buckets and name != "其他"]
    extra = [name for name in buckets if name != "其他" and name not in ordered]
    names = (["其他"] if "其他" in buckets else []) + ordered + extra
    return [(name, buckets[name]) for name in names]


def _numbered_rows(items, day, counter):
    """连续编号 1..N（跨分类不重置），供「3、11、15 都归到购物」这类指令指认。"""
    parts, height = [], 0
    for entry in items:
        counter += 1
        row, row_h = _row(entry, day, {}, show_seq=True, number=counter)
        parts.append(row)
        height += row_h
    return "".join(parts), height, counter


def _next_groups(entries, day, ctx):
    if not entries:
        return "", 0
    parts = ['<section class="no-seq"><h2>下一步</h2>']
    height = SECTION_H
    for name, items in _by_category(entries):
        shown, note = fold(items, FOLD_LIMIT, name)
        parts.append(f'<div class="proj">{html.escape(name)}</div>')
        height += PROJ_H
        inner, inner_h = _rows(shown, day, ctx or {}, show_seq=False)
        parts.append(inner)
        height += inner_h
        if note:
            parts.append(_more(note))
            height += MORE_H
    parts.append("</section>")
    return "".join(parts), height


def _inbox_block(stream, day):
    items = [e for e in stream if e.get("status") == "inbox"]
    if not items:
        return "", 0
    shown, note = fold(items, FOLD_LIMIT, "收件箱")
    inner, inner_h = _rows(shown, day, {}, show_seq=False)
    if note:
        inner += _more(note)
        inner_h += MORE_H
    block, block_h = _section("收件箱 · 待澄清", inner, inner_h,
                              count=len(items) if len(items) > FOLD_LIMIT else None)
    return block, block_h


def _section(title: str, inner_html: str, inner_h: int, count: int | None = None):
    pill = f'<span class="count">{count}</span>' if count else ""
    block = f'<section><h2>{html.escape(title)}{pill}</h2>{inner_html}</section>'
    return block, SECTION_H + inner_h


def _section_rows(title: str, entries, day, ctx=None, show_seq=True):
    inner, inner_h = _rows(entries, day, ctx or {}, show_seq=show_seq)
    if not entries:
        inner, inner_h = '<div class="empty">— 空 —</div>', 52
    return _section(title, inner, inner_h)


# ---- 行与标签 ----

def _rows(entries, day, ctx, show_seq: bool):
    parts, height = [], 0
    for entry in entries:
        row, row_h = _row(entry, day, ctx, show_seq)
        parts.append(row)
        height += row_h
    return "".join(parts), height


def _row(entry, day, ctx, show_seq, number=None):
    tag_text, tag_cls = _tag_text(entry, day, ctx)
    return _row_with(entry, tag_text, tag_cls, show_seq, number)


def _row_with(entry, tag_text: str, tag_cls: str, show_seq: bool, number=None):
    """单行 HTML 与高度。tag 由调用方给定，收件箱视图借此绕过 _tag_text 的时间标签分支。"""
    title = html.escape(entry.get("title") or "")
    tags = _tag(tag_text, tag_cls) if tag_text else ""
    seq = ""
    if show_seq:
        num = number if number is not None else entry.get("seq")
        seq = f'<div class="seq">{num if num is not None else "·"}</div>'
    lines = _est_lines(entry.get("title") or "", len(tag_text) * 34)
    height = ROW_PAD + lines * LINE_H + (TAG_EXTRA_H if tag_text else 0)
    return f'<div class="row">{seq}<div class="c">{title}{tags}</div></div>', height


def _tag_text(entry, day, ctx) -> tuple[str, str]:
    """标签文字与样式类；周期任务在后面加「· 每 N 天」。"""
    text, cls = _base_tag(entry, day, ctx)
    cycle = repeat_label(entry)
    if cycle and text:
        return f"{text} · {cycle}", cls
    return cycle or text, cls


def _base_tag(entry, day, ctx) -> tuple[str, str]:
    """优先级：逾期 > 等待 > 只有 defer > 收件箱天数 > 时间。"""
    due = entry.get("due") or ""
    if is_open(entry) and due and due < day:
        return f"逾期 {_overdue_days(due, day)} 天", "late"
    if entry.get("waiting_on"):
        target = _short_date(due, day) if due else "?"
        return f"等{entry['waiting_on']} · 到 {target}", "wait"
    if not due and entry.get("defer"):
        return f"{_short_date(entry['defer'], day)} 起", ""
    if entry.get("status") == "inbox":
        age = _age_days(entry, day)
        return (f"{age} 天", "late" if age >= 4 else "") if age else ("", "")
    label = when_label(entry, day)
    clock = (entry.get("slot") or "all") != "all"
    if clock and _slot_passed(entry.get("slot"), ctx.get("generated_at")):
        return label, "clock late"
    return label, "clock" if clock else ""


def _tag(text: str, extra: str) -> str:
    cls = f"tag {extra}".strip()
    return f'<span class="{cls}">{html.escape(text)}</span>'


def _overdue_days(due: str, day: str) -> int:
    return max(1, -(_day_delta(due, day) or -1))


def _slot_passed(slot: str, generated_at: str | None) -> bool:
    if not generated_at or ":" not in generated_at:
        return False
    hour, minute = generated_at[11:16].split(":")
    now_minutes = int(hour) * 60 + int(minute)
    if slot == "am":
        return now_minutes > 12 * 60
    if slot == "pm":
        return now_minutes > 18 * 60
    if ":" in slot:
        hh, mm = slot.split(":", 1)[1].split(":")
        return now_minutes > int(hh) * 60 + int(mm)
    return False


def _done_note(done) -> str:
    nums = "、".join(str(e["seq"]) for e in done if e.get("seq") is not None)
    titles = " / ".join(html.escape(e.get("title") or "") for e in done)
    return f'<div class="done-note">{html.escape(nums)} 号已完成 · {titles}</div>'


def _more(note: str) -> str:
    label, rest = note.split("共 ", 1)
    numbers, tail = rest.split(" 条，这里 ", 1)
    return (f'<div class="more">{html.escape(label)}共 <b>{html.escape(numbers)}</b> 条，'
            f'这里 {html.escape(tail)}</div>')


def _est_lines(text: str, extra_px: int = 0) -> int:
    px = sum(33 if ord(ch) > 127 else 18 for ch in text) + extra_px
    return max(1, math.ceil(px / TEXT_W))


# ---- 统计三格与页面骨架 ----

def _stats(stream, day) -> tuple[int, int, int, int]:
    open_items = [e for e in stream if is_open(e)]
    overdue = sum(1 for e in open_items if e.get("due") and e["due"] < day)
    done_today = sum(1 for e in stream if e.get("status") == "done"
                     and (e.get("completed_at") or "")[:10] == day)
    inbox = [e for e in open_items if e.get("status") == "inbox"]
    oldest = max((_age_days(e, day) for e in inbox), default=0)
    return overdue, done_today, len(inbox), oldest


PAGE_CSS = """  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ background: #f7f5f0; }}
  body {{ width: 1080px; height: {height}px; padding: 56px 56px 40px;
         font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
         color: #1b1815; -webkit-font-smoothing: antialiased; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .brand {{ font-size: 24px; letter-spacing: .18em; color: #8a8078; }}
  .date {{ font-size: 26px; color: #8a8078; font-variant-numeric: tabular-nums; }}
  .stats {{ display: flex; margin-top: 30px;
            border-top: 2px solid #d8d2c8; border-bottom: 2px solid #d8d2c8; }}
  .stat {{ flex: 1; padding: 26px 0 22px; }}
  .stat + .stat {{ border-left: 1.5px solid #e0dbd2; padding-left: 34px; }}
  .stat b {{ display: block; font-size: 56px; line-height: 1; color: #123b52;
             font-weight: 700; font-variant-numeric: tabular-nums; }}
  .stat.bad b {{ color: #b4472e; }}
  .stat span {{ display: block; font-size: 22px; color: #8a8078; margin-top: 12px; }}
  section {{ margin-top: 42px; }}
  h2 {{ font-size: 23px; font-weight: 600; letter-spacing: .14em; color: #8a8078;
        margin-bottom: 14px; }}
  h2 .count {{ display: inline-block; min-width: 32px; text-align: center; background: #e0dbd2;
               color: #4a423b; border-radius: 16px; font-size: 20px; padding: 2px 9px;
               margin-left: 10px; letter-spacing: 0; }}
  .row {{ display: flex; align-items: flex-start; padding: 12px 0; }}
  .row + .row {{ border-top: 1px solid #eae5dc; }}
  .seq {{ width: 58px; flex: none; font-size: 31px; font-weight: 600; color: #c3b9ae;
          font-variant-numeric: tabular-nums; padding-top: 1px; }}
  .c {{ font-size: 33px; line-height: 1.42; flex: 1; }}
  .tag {{ display: inline-block; font-size: 20px; padding: 3px 11px; border-radius: 6px;
          margin-left: 12px; vertical-align: 4px; background: #ece7dd; color: #8a8177; }}
  .tag.clock {{ background: #e4ded4; color: #3f362e; }}
  .tag.late {{ background: #f5ddd6; color: #b4472e; font-weight: 600; }}
  .tag.wait {{ background: #e3e8ee; color: #41627c; }}
  .more {{ padding: 12px 0 2px 58px; font-size: 24px; color: #a89e94; }}
  .more b {{ color: #6b6259; font-weight: 600; }}
  .done-note {{ margin-top: 10px; padding: 12px 0 12px 58px; font-size: 24px;
                color: #a89e94; border-top: 1px solid #eae5dc; }}
  .done-note::before {{ content: "✓"; color: #3f6b4a; margin-right: 10px; }}
  .proj {{ font-size: 23px; color: #6b6259; font-weight: 600; margin: 16px 0 4px;
           letter-spacing: .04em; }}
  .no-seq .row {{ padding-left: 0; }}
  .no-seq .more {{ padding-left: 0; }}
  .empty {{ padding: 12px 0; font-size: 24px; color: #b9b0a6; }}
  .intro {{ margin-top: 26px; border-top: 2px solid #d8d2c8; padding: 22px 0 20px; }}
  .intro .blind {{ font-size: 30px; color: #6b6259; }}
  .intro .blind b {{ font-size: 40px; color: #b4472e; font-weight: 700; margin-right: 8px; }}
  .intro .hint {{ margin-top: 12px; font-size: 23px; color: #8a8078; }}
  .boxed {{ background: #f2ece1; border: 1.5px solid #e0dbd2; border-radius: 10px;
            padding: 6px 18px 12px; }}
  .boxed h2 {{ margin-top: 8px; }}
  h2 .count.bad {{ background: #f5ddd6; color: #b4472e; }}
  .foot {{ margin-top: 44px; padding-top: 22px; border-top: 1.5px solid #e0dbd2;
           display: flex; justify-content: space-between; font-size: 21px; color: #a89e94; }}"""

PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{css}
</style></head>
<body>
  <div class="top">
    <div class="brand">{brand}</div>
    <div class="date">{date}{stamp}</div>
  </div>
  {top}
  {blocks}
  <div class="foot">
    <div>{db_name} · 生成时刻 {foot_time}</div>
    <div>序号只管指认，状态全看标签</div>
  </div>
</body></html>"""


def _page(blocks: str, day: str, ctx: dict, height: int, top_html: str,
          brand: str = "GTD 每日看板", show_time: bool = True) -> str:
    clock = html.escape((ctx.get("generated_at") or "")[11:16] or "—")
    stamp = f" · {clock}" if show_time else ""
    return PAGE_HTML.format(css=PAGE_CSS.format(height=height), brand=brand,
                            date=_date_label(day), stamp=stamp, foot_time=clock,
                            top=top_html, blocks=blocks,
                            db_name=html.escape(_db_name(ctx)))


def _db_name(ctx: dict) -> str:
    return Path(ctx.get("db") or "gtd.db").name


def _date_label(day: str) -> str:
    parsed = date.fromisoformat(day)
    return f"{parsed.month}月{parsed.day}日 {WEEKDAY_CN[parsed.weekday()]}"


def _find_chrome() -> str | None:
    env = os.environ.get("GTD_CHROME")
    if env and Path(env).exists():
        return env
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium")


def _screenshot(page: str, out: Path, width: int, height: int) -> None:
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("找不到 Chrome，无法渲染 PNG（可用 GTD_CHROME 指定路径）")
    tmpdir = Path(tempfile.mkdtemp(prefix="gtd-render-"))
    html_path = tmpdir / "board.html"
    html_path.write_text(page, encoding="utf-8")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
           "--force-device-scale-factor=2", f"--user-data-dir={tmpdir / 'profile'}",
           f"--window-size={width},{height}", f"--screenshot={out}", html_path.as_uri()]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"Chrome 截图失败 exit={proc.returncode}\n{proc.stderr[-1500:]}")


register_renderer("json", JsonRenderer)
register_renderer("md", MdRenderer)
register_renderer("png", PngRenderer)


# ================================================================ v2 今日视图（一张图）
#
# 老板定稿：只有这一张图，不再有收件箱视图 / 分类视图（对话里问一句就有）。
# 输入是 Entry 条目流（序号由 inbox.Seq 分配），输出一张 PNG。本节不查库、
# 不调 remindctl、不看系统时钟——now 由调用方注入，纯输入输出。

T_WIDTH = 1080
T_SCALE = 2                      # --force-device-scale-factor=2，出 @2x
T_PAD_X, T_PAD_TOP, T_PAD_BOTTOM = 64, 56, 40
T_TOP_H = 46
T_LINE_H = 44
T_ROW_PAD = 20
T_FOOT_H = 84
T_GAP = 34
T_SAFETY = 16
T_SEQ_W, T_AT_W, T_LIST_W = 54, 146, 110
T_TAG_W = 130
# 列表列留白时不占位：`.title` 是 flex:1，只要不渲染那一块，这 110px 自然归标题。
# 估高也得按同一口径放大标题宽度，否则行数估多、图上多出空白。
# 列表名与所处区块重复时不印：GTD 是默认列表（Siri 都写它，每行重复就是噪音），
# inbox 是收件箱区本身（老板原话「列表 GTD 就没必要再显示了呀」）。
HIDDEN_LIST_NAMES = frozenset({"GTD", "inbox"})
T_TITLE_W = T_WIDTH - T_PAD_X * 2 - T_SEQ_W - T_AT_W - T_LIST_W - T_TAG_W
NEXT_LIMIT = 5                   # 「接下来」区最多 5 条，超出折一行「还有 N 条」
NEXT_DAYS = 7                    # 未来 7 天以内（含第 7 天）才进「接下来」
FAR_LABEL = "更远"               # 超过 NEXT_DAYS 的：图上只折一行，文字视图全量列
# 分区顺序**只此一处**（近 → 远）：图与文字视图都照它排，别再各写一遍。
# 今天 → 接下来（7 天内）→ 更远（7 天外）→ 收件箱（待澄清）。
SECTION_ORDER = ("today", "next", "far", "inbox")
T_WHEN_W = T_SEQ_W + T_AT_W      # 200：「号 + 周五 15:30」正好占序号 + 时刻两列
T_NEXT_LABEL_H = 40
T_MORE_H = 34
# 收件箱区视觉隔离（方案 B）：浅底 + 上方细线，不加任何文字标签。18 + 6 = padding 高。
T_INBOX_BAND_H = 24
ALL_DAY_LABEL = "全天"
# 时段闹钟回显成老板说出口的那个词：146px 的 nowrap 列放不下「全天 · 12:00 响」
ALARM_LABELS = {time(12, 0): "上午", time(18, 0): "下午"}


@dataclass(frozen=True)
class Entry:
    """今日视图的一行。

    ``number`` 是**图上号**，由命令层从 ``board_index`` 取（当天不变、幂等）；
    渲染层只负责印出来，绝不自己编号。``ref`` 是这条背后是谁
    （``reminder:<id>`` / ``inbox:<id>``）：渲染层不用它，命令层拿 ``board_order()``
    写「图上号 → 引用」映射，`done` 靠映射反查。
    """

    number: int
    title: str
    at: time | None
    list_name: str
    overdue_days: int                 # 0 = 不逾期
    kind: str = "task"                # task | inbox
    due: date | None = None           # 这条属于哪天；渲染层据此分「今日」与「接下来」
    ref: str = ""                     # reminder:<id> / inbox:<id>，给 board_index 用
    all_day: bool = False             # Reminder 里的全天条目（at 必为 None）
    alarm: time | None = None         # 全天的闹钟时刻（「12:00 响」），仅用于回显
    priority: str = prio.PRIORITY_NONE   # none/low/medium/high（显示成 ! / !! / !!!）
    repeat: str | None = None         # daily/weekly/…/every N days（标题后印「（每 5 天）」）


def _future(entry: Entry, today: date) -> bool:
    """今天之后的条目（不管多远）。"""
    return entry.kind != "inbox" and entry.due is not None and entry.due > today


def _today_rows(entries: list[Entry], today: date) -> list[Entry]:
    """今日区那几行的唯一口径：今天到期（含逾期）的**任务**，不含收件箱念头。

    顶栏「今天 N 件」与文字视图 ``summary.today`` 都走这里，图上今日区的行也用它切，
    所以「今天 N 件」必然等于今日区真画出来的行数。收件箱行另有「收件箱 K 条」一格，
    不许再重复计进「今天」——以前把它算进来，图上写 2 件、今日区只有 1 行。
    """
    return [entry for entry in entries
            if entry.kind != "inbox" and not _future(entry, today)]


def _upcoming(entry: Entry, today: date) -> bool:
    """今天之后、7 天以内（含第 7 天）的未完成条目 → 进「接下来」区。"""
    if entry.due is None or not _future(entry, today):
        return False
    return (entry.due - today).days <= NEXT_DAYS


def _split_today(entries: list[Entry], today: date):
    """上区：有 at 的按时刻升序在前、无 at 的按传入顺序垫后；下区：inbox。

    未来条目两边都不进——7 天内的归「接下来」，更远的不上图。
    收件箱下区按优先级降序（high → medium → low → none），同档保持传入顺序
    （命令层按 created_at ASC 给的），所以标了感叹号的念头一定排在前面。
    """
    tasks = _today_rows(entries, today)
    timed = [e for e in tasks if e.at is not None]
    timed.sort(key=lambda e: e.at or time(0, 0))
    untimed = [e for e in tasks if e.at is None]
    lower = sorted((e for e in entries if e.kind == "inbox"),
                   key=lambda e: -prio.rank(e.priority))
    return timed + untimed, lower


def _next_key(entry: Entry):
    """due 升序；同日按 at 升序、无时刻的排当天末尾。"""
    return (entry.due, 0 if entry.at is not None else 1, entry.at or time(0, 0))


def _upcoming_sorted(entries: list[Entry], today: date) -> list[Entry]:
    """「接下来」区的全部候选（未来 7 天内），按日期升序。"""
    return sorted((e for e in entries if _upcoming(e, today)), key=_next_key)


def _far_entry(entry: Entry, today: date) -> bool:
    """今天之后、超过 NEXT_DAYS 天的未完成任务 → 归「更远」区。"""
    return (entry.kind != "inbox" and entry.due is not None
            and (entry.due - today).days > NEXT_DAYS)


def _far_sorted(entries: list[Entry], today: date) -> list[Entry]:
    """「更远」区的全部条目（同样按日期升序：最近那条在最前，图上折行取它）。"""
    return sorted((e for e in entries if _far_entry(e, today)), key=_next_key)


def upcoming_entries(entries: list[Entry], *, now: datetime) -> list[Entry]:
    """「接下来」全量（图上只画前 NEXT_LIMIT 条，文字视图全量印）。"""
    return _upcoming_sorted(entries, now.date())


def far_entries(entries: list[Entry], *, now: datetime) -> list[Entry]:
    """「更远」全量（图上只折一行，文字视图全量印）。"""
    return _far_sorted(entries, now.date())


def _when_text(entry: Entry) -> str:
    """时刻列的文字：具体时刻 ``HH:MM``；全天 → ``全天`` / ``上午`` / ``下午``。

    ``all_day`` 有闹钟时用老板说的时段词回显（12:00→上午、18:00→下午），
    非约定时刻退成「全天 09:30」，不拿一个不存在的时段去糊。
    """
    if entry.at is not None:
        return entry.at.strftime("%H:%M")
    if not entry.all_day:
        return ""
    if entry.alarm is None:
        return ALL_DAY_LABEL
    return ALARM_LABELS.get(entry.alarm, f"{ALL_DAY_LABEL} {entry.alarm.strftime('%H:%M')}")


def _next_when(entry: Entry) -> str:
    """日期前缀「周五 15:30」：一眼看出是哪天；全天写「周五 全天」。"""
    label = WEEKDAY_CN[entry.due.weekday()] if entry.due else ""
    return f"{label} {_when_text(entry)}".strip()


def _shown_title(entry: Entry) -> str:
    """图上/表上的标题：优先级感叹号在前、周期括注在后（high !!!、``（每 5 天）``）。"""
    mark = prio.mark(entry.priority)
    title = f"{mark} {entry.title}" if mark else entry.title
    cycle = rep.label(entry.repeat)
    return f"{title}（{cycle}）" if cycle else title


def list_label(entry: Entry) -> str:
    """右侧列表列该印的字；``GTD`` 与收件箱条目返回空串（= 不印、不占位）。

    PNG 的行与 Markdown 的表格列都调这一处，别再各写一遍（上次分区顺序就是这么出错的）。
    """
    if entry.kind == "inbox" or entry.list_name in HIDDEN_LIST_NAMES:
        return ""
    return entry.list_name


def _list_html(entry: Entry) -> str:
    """列表列：留空就整块不渲染，标题把宽度吃下来（不留一块空白把标题挤窄）。"""
    label = list_label(entry)
    return f'<div class="list">{html.escape(label)}</div>' if label else ""


def _next_row(entry: Entry) -> str:
    """接下来区的一行：号是当天固定的图上号，印在日期前缀左边（同一套样式）。"""
    return ('<div class="row">'
            f'<div class="when"><span class="seq">{entry.number}</span>{html.escape(_next_when(entry))}</div>'
            f'<div class="title">{html.escape(_shown_title(entry))}</div>'
            f'{_list_html(entry)}'
            '</div>')


def _next_block(upcoming: list[Entry]):
    """「接下来」区：图上最多 NEXT_LIMIT 条，多出来只折一行；0 条整区不渲染。"""
    if not upcoming:
        return "", 0
    shown = upcoming[:NEXT_LIMIT]
    rows = "".join(_next_row(entry) for entry in shown)
    extra = len(upcoming) - len(shown)
    note = f'<div class="more">还有 {extra} 条</div>' if extra > 0 else ""
    height = T_NEXT_LABEL_H + sum(T_ROW_PAD + _title_lines(e) * T_LINE_H for e in shown)
    height += T_MORE_H if extra > 0 else 0
    return (f'<div class="next"><div class="label">接下来</div>{rows}{note}</div>',
            height)


def _far_date(entry: Entry) -> str:
    """「更远」折行里的日期：只印 ``YYYY-MM-DD``（时刻留给文字视图）。"""
    return entry.due.strftime("%Y-%m-%d") if entry.due else ""


def _far_block(far: list[Entry]):
    """「更远」区：只一行「还有 N 条 · 最近：日期 标题」；0 条整区不渲染。

    样式复用现有折叠行（灰字 ``.more``、无链接无按钮），所以条目再多图也不变长。
    """
    if not far:
        return "", 0
    nearest = far[0]
    note = f"还有 {len(far)} 条 · 最近：{_far_date(nearest)} {nearest.title}"
    return (f'<div class="far"><div class="label">{FAR_LABEL}</div>'
            f'<div class="more">{html.escape(note)}</div></div>',
            T_NEXT_LABEL_H + T_MORE_H)


def _top_bar_html(entries: list[Entry], today: date) -> str:
    """顶栏三格：今日区行数 / 其中逾期的 / 收件箱条数。接下来的、收件箱都不计入「今天」。"""
    sep = '<span class="sep">·</span>'
    todays = _today_rows(entries, today)
    inbox = sum(1 for e in entries if e.kind == "inbox")
    overdue = sum(1 for e in todays if e.overdue_days > 0)
    return (f'<div class="top">今天 <b>{len(todays)}</b> 件{sep}'
            f'逾期 <b>{overdue}</b> 件{sep}收件箱 <b>{inbox}</b> 条</div>')


def _at_html(entry: Entry, now: datetime) -> str:
    """时刻列：inbox / 无时刻留空；早于 now（未完成）显示红褐色 #b4472e。

    全天条目永远不上红色：它不是逾期，只是「整天挂着」，用比普通时刻更淡的灰。
    """
    if entry.kind == "inbox":          # 收件箱的念头没有时间，绝不印时刻
        return '<div class="at"></div>'
    text = _when_text(entry)
    if not text:
        return '<div class="at"></div>'
    if entry.all_day or entry.at is None:
        return f'<div class="at allday">{text}</div>'
    css = "at past" if entry.at < now.time() else "at"
    return f'<div class="{css}">{text}</div>'


def _tag_html(entry: Entry) -> str:
    """逾期只做标签，不给整行上色、不给序号上色。"""
    if entry.overdue_days <= 0:
        return ""
    return f'<span class="tag">逾期 {entry.overdue_days} 天</span>'


def _entry_row(entry: Entry, now: datetime) -> str:
    return ('<div class="row">'
            f'<div class="seq">{entry.number}</div>'
            f'{_at_html(entry, now)}'
            f'<div class="title">{html.escape(_shown_title(entry))}</div>'
            f'{_tag_html(entry)}'
            f'{_list_html(entry)}'
            '</div>')


def _rows_block(css_class: str, entries: list[Entry], now: datetime) -> str:
    if not entries:
        return ""
    rows = "".join(_entry_row(entry, now) for entry in entries)
    return f'<div class="{css_class}">{rows}</div>'


def _title_width(entry: Entry) -> int:
    """标题可用宽度：右侧不印列表列时，那 110px 归标题（不按老宽度估行数）。"""
    return T_TITLE_W if list_label(entry) else T_TITLE_W + T_LIST_W


def _title_lines(entry: Entry) -> int:
    px = sum(31 if ord(ch) > 127 else 16 for ch in _shown_title(entry))
    return max(1, math.ceil(px / _title_width(entry)))


def _rows_height(entries: list[Entry]) -> int:
    """一组行的估算高度（与 ``_rows_block`` 对应）。"""
    return sum(T_ROW_PAD + _title_lines(e) * T_LINE_H for e in entries)


def _today_estimate(heights: dict[str, int]) -> int:
    """按各区高度估高；真实高度由 Chrome 量（见 _measure_height），这里只是兜底。"""
    height = T_PAD_TOP + T_TOP_H + T_FOOT_H + T_PAD_BOTTOM + T_SAFETY
    for name in SECTION_ORDER:
        if heights.get(name):
            height += T_GAP + heights[name]
    return height


def _today_document(body: str) -> str:
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><style>\n'
            + TODAY_CSS + '\n</style></head>\n<body>\n' + body
            + '\n<div id="probe" style="display:none">0</div>'
            '\n<script>document.getElementById("probe").textContent=String('
            'Math.ceil(document.body.getBoundingClientRect().height));</script>'
            '\n</body></html>\n')


def _layout(entries: list[Entry], today: date):
    """图上分区的唯一真相：今日上区 / 接下来全量 / 更远全量 / 收件箱下区。"""
    upper, lower = _split_today(entries, today)
    return (upper, _upcoming_sorted(entries, today),
            _far_sorted(entries, today), lower)


def board_order(entries: list[Entry], *, now: datetime) -> list[Entry]:
    """图上从上到下真画出来的那一份顺序（今日 → 接下来前 5 → 收件箱）。

    图上 1..N 的号就按它排；命令层拿它写「图上号 → ref」映射，`done` 靠映射反查。
    「更远」只折一行、不逐条画，所以不在这里；它的号由命令层单独追加（见 cli）。
    """
    upper, upcoming, _far, lower = _layout(entries, now.date())
    return upper + upcoming[:NEXT_LIMIT] + lower


def _page_sections(entries: list[Entry], now: datetime):
    """(名字, HTML, 高度) 三段——顺序由 ``SECTION_ORDER`` 一处决定，图与文字共用。"""
    upper, upcoming, far, lower = _layout(entries, now.date())
    made = {
        "today": (_rows_block("rows", upper, now), _rows_height(upper)),
        "next": _next_block(upcoming),
        "far": _far_block(far),
        "inbox": (_rows_block("lower", lower, now),
                  _rows_height(lower) + (T_INBOX_BAND_H if lower else 0)),
    }
    return [(name, made[name][0], made[name][1]) for name in SECTION_ORDER]


def build_today_html(entries: list[Entry], *, now: datetime) -> tuple[str, int, int]:
    """返回 (html, 宽, 高)；高是估算值，render_today 会再用 Chrome 量一次。

    区块顺序只由 ``SECTION_ORDER`` 决定：今日（含逾期）→ 接下来 → 更远 → 收件箱。
    序号直接用 ``Entry.number``（命令层按 board_index 当天固定号给），这里不重排。
    """
    sections = _page_sections(entries, now)
    body = (_top_bar_html(entries, now.date())
            + "".join(html_ for _name, html_, _h in sections)
            + f'<div class="foot">只读 · {now.strftime("%H:%M")}</div>')
    heights = {name: height for name, _html, height in sections}
    return _today_document(body), T_WIDTH, _today_estimate(heights)


def render_today(entries: list[Entry], *, now: datetime, out_path: Path) -> Path:
    """出一张今日视图 PNG，返回文件路径。临时 HTML 写在 out 同目录、截图后删掉。"""
    page, width, height = build_today_html(entries, now=now)
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    html_path = _write_temp_html(page, out.parent)
    try:
        _shoot_today(html_path, out, width, height)
    finally:
        html_path.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------- v2 文字视图（Markdown / JSON）
#
# 与 `render_today` 同一份数据装配：三区（今天 → 接下来 → 收件箱）、同一套图上号。
# 区别只在最后一公里——图是 PNG，文字是 Markdown（手机能直接看）或 JSON（脚本用）。

MD_NEXT_LABEL = "**接下来**"
MD_FAR_LABEL = "**更远**"
MD_INBOX_LABEL = "**收件箱**"


def _counts(entries: list[Entry], today: date) -> dict[str, int]:
    """顶栏三个数，与图上「今天 N 件 · 逾期 M 件 · 收件箱 K 条」**同一口径**。

    三个数都出自 ``_today_rows`` / ``kind``：``today`` 恒等于 ``today`` 数组长度
    （今日区真画出来的行数），收件箱另有自己一格、不重复计入。图与文字都走这一处，
    所以「今天 N 件」永远能一条条数出来。
    """
    todays = _today_rows(entries, today)
    return {"today": len(todays),
            "overdue": sum(1 for entry in todays if entry.overdue_days > 0),
            "inbox": sum(1 for entry in entries if entry.kind == "inbox")}


def _md_summary(entries: list[Entry], today: date) -> str:
    counts = _counts(entries, today)
    return (f"**今天 {counts['today']} 件 · 逾期 {counts['overdue']} 件 · "
            f"收件箱 {counts['inbox']} 条**")


def _md_title(entry: Entry) -> str:
    """标题单元格：优先级感叹号在前，逾期条目在后面加「（逾期 N 天）」。"""
    head = _shown_title(entry)
    if entry.overdue_days <= 0:
        return head
    return f"{head}（逾期 {entry.overdue_days} 天）"


def _md_cell(value) -> str:
    """表格单元格：竖线必须转义（否则把表格劈开），换行压成空格。"""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(head: tuple[str, ...], rows: list[tuple]) -> str:
    """第一列恒为图上号 ``#``，其余列按 ``head``；行里的竖线统一转义。"""
    cols = ("#", *head)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_md_cell(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _far_when(entry: Entry) -> str:
    """更远区的日期列：``YYYY-MM-DD HH:MM``；全天条目只给日期（有闹钟给钟点）。

    「更远」跨度可能是几个月到几年，光写「周五 16:00」看不出年份，所以印完整日期。
    """
    if entry.due is None:
        return ""
    stamp = entry.due.strftime("%Y-%m-%d")
    clock = entry.at or (entry.alarm if entry.all_day else None)
    return f"{stamp} {clock.strftime('%H:%M')}" if clock else stamp


def _md_entry_table(when_head: str, entries: list[Entry], when_fn) -> str:
    """今日/接下来/更远共用的表：``| # | 时刻 | 标题 | [列表] |``。

    「列表」列按 ``list_label()`` 定：整区一条可印的都没有（全 GTD / 全是收件箱）就
    **整列不出现**（标题自然变宽），有 Waiting 这类的才补上这一列、其余格留空。
    """
    show_list = any(list_label(entry) for entry in entries)
    head = (when_head, "标题", "列表") if show_list else (when_head, "标题")
    rows = []
    for entry in entries:
        cells = (str(entry.number), when_fn(entry), _md_title(entry))
        rows.append(cells + (list_label(entry),) if show_list else cells)
    return _md_table(head, rows)


def _md_next_block(upcoming: list[Entry]) -> str:
    """「接下来」区**全量**上表（老板要全量：50 条也不截断、不写「还有 N 条」）。"""
    return _md_entry_table("日期时刻", upcoming, _next_when)


def _md_far_block(far: list[Entry]) -> str:
    """「更远」区全量上表：日期升序，号照 ``board_index`` 原样印。"""
    return _md_entry_table("日期", far, _far_when)


def _md_today_block(upper: list[Entry]) -> str:
    """今日表（含逾期）；空区返回空串。"""
    if not upper:
        return ""
    return _md_entry_table("时刻", upper, _when_text)


def _md_inbox_block(lower: list[Entry]) -> str:
    """收件箱表；空区返回空串。"""
    if not lower:
        return ""
    rows = [(str(entry.number), _md_title(entry)) for entry in lower]
    return f"{MD_INBOX_LABEL}\n\n{_md_table(('标题',), rows)}"


def _md_sections(entries: list[Entry], now: datetime) -> list[str]:
    """各区 Markdown；顺序由 ``SECTION_ORDER`` 一处决定（与图同一份顺序）。"""
    upper, upcoming, far, lower = _layout(entries, now.date())
    made = {
        "today": _md_today_block(upper),
        "next": f"{MD_NEXT_LABEL}\n\n{_md_next_block(upcoming)}" if upcoming else "",
        "far": f"{MD_FAR_LABEL}\n\n{_md_far_block(far)}" if far else "",
        "inbox": _md_inbox_block(lower),
    }
    return [made[name] for name in SECTION_ORDER if made[name]]


def render_today_markdown(entries: list[Entry], *, now: datetime) -> str:
    """全库 Markdown：今天 → 接下来 → 更远 → 收件箱；空区整区不渲染、全量不截断。

    编号由命令层按 ``board_index`` 填在 ``Entry.number``（与 ``board`` 出图同一套号），
    这里只印不排。逾期条目标题后带「（逾期 N 天）」，全天/上午/下午沿用 ``_when_text``。
    """
    blocks = [_md_summary(entries, now.date()), *_md_sections(entries, now)]
    return "\n\n".join(blocks) + "\n"


def _view_item(entry: Entry, when: str) -> dict:
    return {"number": entry.number, "when": when, "title": entry.title,
            "list": entry.list_name, "overdue_days": entry.overdue_days,
            "priority": prio.normalize(entry.priority),
            "repeat": rep.normalize(entry.repeat)}


def render_today_json(entries: list[Entry], *, now: datetime) -> str:
    """结构化文字视图：四区条目 + 每条的号/时刻/标题/列表/逾期天数（全量，不截断）。"""
    upper, upcoming, far, lower = _layout(entries, now.date())
    payload = {"summary": _counts(entries, now.date()),
               "next_total": len(upcoming), "far_total": len(far),
               "today": [_view_item(e, _when_text(e)) for e in upper],
               "next": [_view_item(e, _next_when(e)) for e in upcoming],
               "far": [_view_item(e, _far_when(e)) for e in far],
               "inbox": [_view_item(e, "") for e in lower]}
    return json.dumps(payload, ensure_ascii=False, indent=2)


VIEW_RENDERERS = {"md": render_today_markdown, "json": render_today_json}


def view_formats() -> tuple[str, ...]:
    """文字视图可选的格式名（命令层据此给 argparse choices，不写死分支）。"""
    return tuple(sorted(VIEW_RENDERERS))


def render_view(entries: list[Entry], *, now: datetime, fmt: str = "md") -> str:
    """按格式渲染文字视图（``md`` 默认 / ``json``）；未知格式抛 ValueError。"""
    try:
        renderer = VIEW_RENDERERS[fmt]
    except KeyError as exc:
        raise ValueError(f"未知格式：{fmt}（可选：{'/'.join(view_formats())}）") from exc
    return renderer(entries, now=now)


def _write_temp_html(page: str, folder: Path) -> Path:
    handle, name = tempfile.mkstemp(prefix=".gtd-today-", suffix=".html", dir=folder)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(page)
    return Path(name)


def _shoot_today(html_path: Path, out: Path, width: int, height: int) -> None:
    """先量真实内容高度再截；截完核对 PNG 高度，裁了就加高重截一次。"""
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("找不到 Chrome，无法渲染 PNG（可用 GTD_CHROME 指定路径）")
    need = _measure_height(chrome, html_path, width) or height
    for target in (need, need + 240):
        _chrome_screenshot(chrome, html_path, out, width, target)
        if _png_size(out)[1] >= need * T_SCALE:
            return
    raise RuntimeError(f"截图高度不足（需要 {need * T_SCALE}px）：{_png_size(out)}")


def _chrome_args(chrome: str, profile: str, width: int, height: int) -> list[str]:
    return [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", f"--user-data-dir={profile}",
            f"--window-size={width},{height}"]


def _measure_height(chrome: str, html_path: Path, width: int) -> int | None:
    """用 --dump-dom 让页面自己报 scrollHeight；拿不到返回 None（退回估算）。"""
    with tempfile.TemporaryDirectory(prefix="gtd-measure-") as profile:
        cmd = _chrome_args(chrome, profile, width, 900) + [
            "--virtual-time-budget=2000", "--dump-dom", html_path.as_uri()]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.SubprocessError:
            return None
    found = re.search(r'id="probe"[^>]*>(\d+)<', proc.stdout)
    return int(found.group(1)) if found else None


def _chrome_screenshot(chrome: str, html_path: Path, out: Path,
                       width: int, height: int) -> None:
    with tempfile.TemporaryDirectory(prefix="gtd-shot-") as profile:
        cmd = _chrome_args(chrome, profile, width, height) + [
            f"--force-device-scale-factor={T_SCALE}", f"--screenshot={out}",
            html_path.as_uri()]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"Chrome 截图失败 exit={proc.returncode}\n{proc.stderr[-1200:]}")


def _png_size(path: Path) -> tuple[int, int]:
    """直接读 PNG 的 IHDR：不引第三方库也能知道截图多高。"""
    with open(path, "rb") as handle:
        head = handle.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"不是 PNG 文件：{path}")
    return struct.unpack(">II", head[16:24])


TODAY_CSS = """  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: #f7f5f0; }
  body { width: 1080px; padding: 56px 64px 40px;
         font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
         color: #1b1815; -webkit-font-smoothing: antialiased; }
  .top { font-size: 24px; color: #8a8078; letter-spacing: .02em; }
  .top b { font-weight: 600; color: #6b6259; font-variant-numeric: tabular-nums; }
  .top .sep { margin: 0 16px; color: #cfc7bd; }
  .rows { margin-top: 34px; }
  .lower { margin: 44px -64px 0; padding: 18px 64px 6px; background: #f1ede4;
           border-top: 1px solid #cfc7bd; }
  .next { margin-top: 34px; }
  .far { margin-top: 34px; }
  .label { font-size: 22px; color: #8a8078; letter-spacing: .14em; margin-bottom: 2px; }
  .when { width: 200px; flex: none; font-size: 24px; color: #6b6259; white-space: nowrap;
          font-variant-numeric: tabular-nums; }
  .when .seq { display: inline-block; width: 54px; }
  .more { margin-top: 2px; font-size: 22px; color: #a89e94; }
  .row { display: flex; align-items: baseline; padding: 10px 0; }
  .seq { width: 54px; flex: none; font-size: 24px; color: #c3b9ae;
         font-variant-numeric: tabular-nums; }
  .at { width: 146px; flex: none; font-size: 26px; color: #6b6259;
        font-variant-numeric: tabular-nums; }
  .at.past { color: #b4472e; }
  .at.allday { color: #8a8078; }
  .title { flex: 1; font-size: 31px; line-height: 1.4; }
  .tag { flex: none; margin-left: 14px; font-size: 20px; color: #b4472e;
         background: #f4ded6; border-radius: 6px; padding: 3px 10px; }
  .list { width: 110px; flex: none; text-align: right; font-size: 21px; color: #a89e94; }
  .foot { margin-top: 52px; font-size: 20px; color: #b3a99f; }"""
