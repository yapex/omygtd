"""渲染层：把「统一条目流」渲染成 json / md / png。

职责边界（S/I）：只认条目结构，不查库、不 import sqlite3、不认识命令参数。
三种格式是三个策略类，共用 `render(stream, view, ctx) -> Artifact` 一个方法；
新增格式只需 register_renderer()，不改任何现有类（O），命令层只查注册表（L）。
"""

from __future__ import annotations

import html
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

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
