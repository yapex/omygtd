"""命令层 v2：add / inbox / clarify / board / done / list。

老板经对话让我调 CLI（不是自己敲），所以**默认输出 JSON**（机器读），
``--human`` 才给人看。退出码：0 成功 / 1 业务失败 / 2 参数错误。

依赖注入：
- ``store``：``ReminderStore`` 从外面传进来（测试传 ``FakeStore``），本模块不认识 remindctl
- ``now``：``--now "2026-11-23 09:00"`` 或 ``main(now=...)`` 钉死时钟（测试用）
- ``--db``：本地库路径（inbox / daily_seq / mirror），默认 ``data/gtd.db``，也认 ``GTD_DB``

分工：
- **有时间的任务全在 Reminder**；本地只记 inbox（没时间的念头）、当日序号、镜像。
- Reminder 写入失败时**念头不许丢**：留在 inbox，返回 ``ok:false`` + 退出码 1。
- 号（``daily_seq``）是老板唯一认的编号：``board`` / ``inbox`` 印它，``clarify`` /
  ``done`` 也按它找人；还没拿到号的念头可以退回按 inbox 行 id 找。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import __version__, doctor, parser as nlp, priority as prio, repeat as rep, render
from .inbox import DEFAULT_DB, PROJECT_ROOT, BoardIndex, Inbox, Mirror, Seq
from .reminders import RemindctlAdapter, ReminderStore

EXIT_OK, EXIT_FAIL, EXIT_PARAM = 0, 1, 2
LISTS = (nlp.LIST_DEFAULT, nlp.LIST_WAITING)
OUT_ENV = "GTD_OUT"
STAMP_FMT = "%Y-%m-%d %H:%M"
INBOX_LIST_LABEL = "inbox"
SLOT_WORDS = {"am": "上午", "pm": "下午"}      # --human 回执用的时段词
ALL_DAY_WORD = "全天"


@dataclass
class Result:
    payload: dict
    code: int = EXIT_OK


@dataclass
class Context:
    """一次命令执行的上下文：本地库四张表 + Reminder 存储 + 钉住的时刻。"""

    inbox: Inbox
    seq: Seq
    board: BoardIndex
    store: ReminderStore
    now: datetime

    @property
    def today(self) -> date:
        return self.now.date()


# ---------------------------------------------------------------- 入口

def main(argv=None, *, store: ReminderStore | None = None, now=None) -> int:
    """跑一条命令，返回退出码（0 成功 / 1 业务失败 / 2 参数错误）。"""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:          # argparse：未知命令、缺参数 → 2；--help → 0
        return int(exc.code or 0)
    try:
        ctx = _context(args, store, now)
        result = DISPATCH[args.command](ctx, args)
    except (RuntimeError, ValueError, OSError) as exc:
        _write({"ok": False, "error": str(exc)}, args.human, args.command)
        return EXIT_FAIL
    _write(result.payload, args.human, args.command)
    return result.code


def _context(args, store, now) -> Context:
    moment = _moment(now if now is not None else args.now)
    db = args.db or os.environ.get("GTD_DB") or DEFAULT_DB
    return Context(Inbox(db), Seq(db), BoardIndex(db),
                   store if store is not None else RemindctlAdapter(), moment)


def _write(payload: dict, human: bool, command: str = "") -> None:
    text = payload.get("text")            # view：渲染好的文字直接铺出去，不套 JSON
    if text is None:
        text = _human(command, payload) if human else json.dumps(payload, ensure_ascii=False,
                                                                 indent=2)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _moment(raw) -> datetime:
    """``None`` / datetime / 'YYYY-MM-DD[ HH:MM[:SS]]' → datetime（本地时区口径）。"""
    if raw is None:
        return datetime.now()
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"--now 认不出这个时刻：{raw}")


# ---------------------------------------------------------------- 小工具

def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _list_of(parsed) -> str:
    return parsed.list if parsed.list in LISTS else nlp.LIST_DEFAULT


def _due_at(parsed, now: datetime) -> datetime | None:
    """解析结果 → Reminder 的到期时刻；只有日期时落当天零点（全天由标记决定）。

    全天 / 时段的「没有钟点」不是 22:30：`parsed.all_day` / `parsed.slot` 直接传给
    存储层，这里只给日期占位。什么时间都没听出来 → None（进收件箱）。
    """
    if parsed.due is None and parsed.at is None:
        return None
    return datetime.combine(parsed.due or now.date(), parsed.at or time(0, 0))


def _advance(parsed) -> int | None:
    """闹钟提前量：具体时刻默认到点即响（0），「提前X」优先；全天/时段交给存储层。"""
    if parsed.at is None:
        return parsed.advance_minutes
    return parsed.advance_minutes if parsed.advance_minutes is not None else 0


def _ensure_lists(store: ReminderStore) -> None:
    """真实实现里缺列表要建（GTD / Waiting）；FakeStore 里是空操作。"""
    store.ensure_lists()


def _seq_of(ctx: Context, ref: str) -> int:
    """同一个 ref 当天只占一个号：已分配过就复用，否则新分配。"""
    for seq, known in ctx.seq.all(day=ctx.today).items():
        if known == ref:
            return seq
    return ctx.seq.assign(ref, day=ctx.today)


def _stamp_of(value) -> datetime | None:
    """本地墙钟串 → datetime；认不出给 None（"YYYY-MM-DD" 落在当天 00:00）。"""
    text = str(value or "").strip().replace("T", " ").replace("Z", "")[:19]
    for fmt in (STAMP_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _item_due(item: dict) -> datetime | None:
    """remindctl / FakeStore 的 dueDate → datetime；认不出给 None。"""
    return _stamp_of(item.get("dueDate") or item.get("due"))


def _item_alarm(item: dict, due: datetime) -> time | None:
    """同一时刻响的闹钟 → 时刻；跨日的（提前到前一天）不算在图上的时刻列。"""
    alarm = _stamp_of(item.get("alarmDate"))
    if alarm is None or alarm.date() != due.date():
        return None
    return alarm.time()


def _age_days(row: dict, now: datetime) -> int:
    try:
        created = datetime.fromisoformat(str(row.get("created_at"))).date()
    except ValueError:
        return 0
    return max(0, (now.date() - created).days)


def _out_root() -> Path:
    return Path(os.environ.get(OUT_ENV) or PROJECT_ROOT / "out")


def _default_out(day: date) -> Path:
    """当天最新副本：``out/today-YYYYMMDD.png``（cron / 人可直接引用这个固定路径）。"""
    return _out_root() / f"today-{day.strftime('%Y%m%d')}.png"


def _unique_out(stamp: datetime) -> Path:
    """带时刻的唯一名：``out/board-YYYYMMDD-HHMMSS.png``，供推送绕开客户端缓存。

    同一秒内连出两次图会撞名（撞了客户端又按路径缓存），撞到已存在的就顺延一秒。
    """
    root = _out_root()
    candidate = root / f"board-{stamp.strftime('%Y%m%d-%H%M%S')}.png"
    for step in range(60):
        if not candidate.exists():
            return candidate
        moment = stamp + timedelta(seconds=step + 1)
        candidate = root / f"board-{moment.strftime('%Y%m%d-%H%M%S')}.png"
    return candidate


# ---------------------------------------------------------------- add

def cmd_add(ctx: Context, args) -> Result:
    """有时间进 Reminder，没时间进 inbox；Reminder 失败就留在 inbox。"""
    parsed = nlp.parse(args.text, now=ctx.now)
    level = args.priority or parsed.priority          # 命令行 --priority 覆盖解析结果
    due_at = _due_at(parsed, ctx.now)
    if due_at is None:
        return _park(ctx, parsed, args.note, level=level)
    _ensure_lists(ctx.store)
    reminder_id = ctx.store.add(parsed.title, due_at, _list_of(parsed),
                                advance_minutes=_advance(parsed),
                                note=args.note,
                                all_day=parsed.all_day, slot=parsed.slot,
                                priority=level, repeat=parsed.repeat)
    if not reminder_id:
        return _park(ctx, parsed, args.note, due_at=due_at, level=level,
                     error="Reminder 写不进去，已留在收件箱")
    return Result(_landed(ctx, parsed, reminder_id, due_at, level=level))


def _landed(ctx: Context, parsed, reminder_id: str, due_at: datetime, title=None,
            level=None) -> dict:
    """写成功后的回执：``at`` 只报具体时刻，全天/时段另给标记（别把 00:00 当时刻）。"""
    return {"ok": True, "seq": _seq_of(ctx, f"reminder:{reminder_id}"),
            "title": title or parsed.title, "list": _list_of(parsed),
            "due": due_at.date().isoformat(),
            "at": _hhmm(due_at) if parsed.at is not None else None,
            "all_day": parsed.all_day, "slot": parsed.slot,
            "priority": prio.normalize(level or parsed.priority),
            "repeat": parsed.repeat,
            "reminder_id": reminder_id, "matched": parsed.matched,
            "source": "reminder"}


def _park(ctx: Context, parsed, note, error: str | None = None,
          due_at: datetime | None = None, level=None) -> Result:
    """念头落 inbox（不丢），迁出标记留给 clarify / done。"""
    level = prio.normalize(level or parsed.priority)
    inbox_id = ctx.inbox.add(parsed.title, note or "", now=ctx.now, priority=level)
    payload = {"ok": error is None, "seq": _seq_of(ctx, f"inbox:{inbox_id}"),
               "title": parsed.title, "list": None, "source": "inbox",
               "due": due_at.date().isoformat() if due_at else None,
               "at": _hhmm(due_at) if due_at and parsed.at is not None else None,
               "all_day": parsed.all_day, "slot": parsed.slot,
               "priority": level,
               "repeat": parsed.repeat,
               "inbox_id": inbox_id, "matched": parsed.matched}
    if error:
        payload["error"] = error
    return Result(payload, code=EXIT_OK if error is None else EXIT_FAIL)


# ---------------------------------------------------------------- inbox

def cmd_inbox(ctx: Context, _args) -> Result:
    rows = ctx.inbox.pending()
    known = {ref: seq for seq, ref in ctx.seq.all(day=ctx.today).items()}
    items = [{"id": row["id"], "seq": known.get(f"inbox:{row['id']}"),
              "title": row["title"], "note": row["note"],
              "created_at": row["created_at"],
              "priority": prio.normalize(row.get("priority")),
              "age_days": _age_days(row, ctx.now)} for row in rows]
    return Result({"ok": True, "count": len(items),
                   "oldest_days": ctx.inbox.oldest_age_days(now=ctx.now),
                   "items": items})


# ---------------------------------------------------------------- clarify

def cmd_clarify(ctx: Context, args) -> Result:
    """给 inbox 里的念头补时间：Reminder 成功才打迁出标记。"""
    row = _pending_row(ctx, args.number)
    if row is None:
        return Result({"ok": False,
                       "error": f"收件箱里没有 #{args.number}（可能已定时间，或者号不存在）"},
                      code=EXIT_FAIL)
    parsed = nlp.parse(args.text, now=ctx.now)
    due_at = _due_at(parsed, ctx.now)
    if due_at is None:
        return Result({"ok": False, "error": "没听出时间", "inbox_id": row["id"]},
                      code=EXIT_FAIL)
    title = parsed.title or row["title"]
    level = parsed.priority if prio.is_set(parsed.priority) else prio.normalize(row.get("priority"))
    _ensure_lists(ctx.store)
    reminder_id = ctx.store.add(title, due_at, _list_of(parsed),
                                advance_minutes=_advance(parsed),
                                note=row["note"],
                                all_day=parsed.all_day, slot=parsed.slot,
                                priority=level, repeat=parsed.repeat)
    if not reminder_id:
        return Result({"ok": False, "error": "Reminder 写不进去，念头留在收件箱",
                       "inbox_id": row["id"]}, code=EXIT_FAIL)
    ctx.inbox.mark_moved(row["id"], reminder_id, now=ctx.now)
    payload = _landed(ctx, parsed, reminder_id, due_at, title=title, level=level)
    payload["inbox_id"] = row["id"]
    return Result(payload)


def _pending_row(ctx: Context, number: int) -> dict | None:
    """按「老板看到的号」找：先看当日序号（board / inbox 上印的号），再退回 inbox 行 id。

    日常路径是序号——老板在图上看到 3，就说「3 明天上午」。行 id 是兜底：
    还没拿到号的念头（seq 为空）也能直接按 id 澄清。
    """
    ref = ctx.seq.lookup(number, day=ctx.today)
    pending = ctx.inbox.pending()
    if ref is not None:
        if not ref.startswith(f"{INBOX_LIST_LABEL}:"):
            return None                      # 这个号被 Reminder 里的任务占了
        target = int(ref.partition(":")[2])
        return next((row for row in pending if row["id"] == target), None)
    return next((row for row in pending if row["id"] == number), None)


# ---------------------------------------------------------------- 视图数据装配

@dataclass
class TodayView:
    """一次「看板/文字视图」的装配结果：Reminder 全量、图上条目、当天固定号。

    ``items`` 是 GTD/Waiting 未完成的全部条目（镜像要全量）；``entries`` 是图上/表上
    会出现的条目（``number`` 已按 ``board_index`` 填好）；``order`` 是图上从上到下的
    顺序（今日 → 接下来 → 收件箱），``numbers`` 是这条顺序上每条的号。
    """

    items: list
    entries: list
    order: list
    numbers: dict


def _today_view(ctx: Context) -> TodayView:
    """读 Reminder → 组 Entry → 分区 → 分配当天图上号。board 与 view 共用这一份。

    两个命令的号之所以一定一致，就是因为装配只此一处：谁先跑谁写 ``board_index``，
    后跑的读到同一批号（当天幂等）。
    """
    items = _open_items(ctx)                    # 出图与镜像共用这一份，不读两遍
    entries = _today_tasks(ctx, items) + _inbox_entries(ctx)
    order = render.board_order(entries, now=ctx.now)      # 图上从上到下
    numbers = _board_numbers(ctx, order)                 # 引用 → 当天固定的图上号
    numbered = [replace(entry, number=numbers.get(entry.ref, entry.number))
                for entry in entries]     # 没上图（超过 5 条）的条目不会印出来
    return TodayView(items=items, entries=numbered, order=order, numbers=numbers)


# ---------------------------------------------------------------- board

def cmd_board(ctx: Context, args) -> Result:
    """今天该看的：Reminder 今天到期（含逾期）+ inbox 全部，出一张图。

    图上号（``board_index``）**当天不变**：老条目沿用上次的号，新条目从
    ``max(display_seq)+1`` 往后追加，消失的条目只删行、不重排。所以同样的数据
    连出多少次图，号一个字都不变；`done N` 就能一直指到同一条。

    出图**成功之后**才写：整表重建 mirror（``drift`` 于是变成「上次出图以后
    Reminder 被改过」的信号）＋图上号映射。顺序不能反：出图失败就不写，
    否则会记下一个用户根本没看到的视图。两处写失败只进 ``warnings``，board 仍成功。

    默认落两个文件：唯一名 ``out/board-YYYYMMDD-HHMMSS.png``（``path``，推送用
    它绕开客户端按路径缓存）＋当天最新副本 ``out/today-YYYYMMDD.png``（``latest``，
    供 cron / 人直接引用）。``--out`` 显式给了就只写那一个，``latest`` 为 None。
    """
    view = _today_view(ctx)
    explicit = Path(args.out).expanduser() if args.out else None
    out = explicit or _unique_out(datetime.now())      # 时刻取本地墙钟，不是 UTC
    out.parent.mkdir(parents=True, exist_ok=True)
    path = render.render_today(view.entries, now=ctx.now, out_path=out)
    latest = None if explicit else _default_out(ctx.today)
    if latest is not None:                     # 实体副本（copy2），不用软链
        latest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, latest)
    payload = {"ok": True, "path": str(path), "latest": str(latest) if latest else None,
               "count": len(view.order),
               "overdue": sum(1 for entry in view.order if entry.overdue_days > 0),
               "inbox": sum(1 for entry in view.order if entry.kind == "inbox"),
               "numbers": [view.numbers[entry.ref] for entry in view.order]}
    warnings = _persist_board(ctx, view.items, view.numbers)
    if warnings:
        payload["warnings"] = warnings
    return Result(payload)


# ---------------------------------------------------------------- view

def cmd_view(ctx: Context, args) -> Result:
    """全库文字视图：Markdown（默认，手机上看）或 JSON（脚本用）。

    与 ``board`` 同一份装配（``_today_view``），所以同一数据状态下两边的号一模一样；
    成功后同样增量写 ``board_index``（幂等规则不变）——「看了文字说 2 号完成」也能定位。
    渲染文本放在 ``text`` 里，``_write`` 直接铺到 stdout（不再套一层 JSON）。
    """
    view = _today_view(ctx)
    fmt = getattr(args, "format", "md") or "md"
    payload = {"ok": True, "format": fmt, "count": len(view.order),
               "numbers": [view.numbers[entry.ref] for entry in view.order],
               "text": render.render_view(view.entries, now=ctx.now, fmt=fmt)}
    warnings = _persist_board(ctx, view.items, view.numbers)
    if warnings:
        payload["warnings"] = warnings
    return Result(payload)


def _board_numbers(ctx: Context, order) -> dict[str, int]:
    """按今天的映射给每条挑号：已有的原样沿用，没有的从 max+1 往后追加。

    ``order`` 是图上从上到下的顺序——只影响**新号**的先后，老号不会被它拉动。
    """
    known = ctx.board.numbered(day=ctx.today)
    numbers = {entry.ref: known[entry.ref] for entry in order if entry.ref in known}
    nxt = ctx.board.next_number(day=ctx.today)
    for entry in order:
        if entry.ref not in numbers:
            numbers[entry.ref] = nxt
            nxt += 1
    return numbers


def _open_items(ctx: Context) -> list[tuple[str, dict]]:
    """``GTD`` / ``Waiting`` 里未完成的全部条目（含明天以后）：镜像要全量。"""
    return [(name, item) for name in LISTS for item in ctx.store.pending(name)]


def _today_tasks(ctx: Context, items) -> list:
    """今天该看的（含逾期）＋ 未来 7 天内的「接下来」；更远的不传。

    未来条目也带上 ``due``：渲染层靠它把「今日」和「接下来」分开，并印出
    「周五 15:30」这样的日期前缀。窗口宽度取 ``render.NEXT_DAYS``，只在渲染层定义一次。
    每条都带 ``ref``：图上号就靠它跟 ``board_index`` 对上。
    """
    return [entry for entry in (_reminder_entry(name, item, ctx.today)
                                for name, item in items) if entry is not None]


def _reminder_entry(name: str, item: dict, today: date):
    """一条 Reminder → 图上的一行；太远（超过 7 天）或没有 due 的返回 None。

    全天条目 ``at=None`` + ``all_day=True``：**不许把 00:00 当时刻**（EventKit 存的
    全天就是本地零点）。时段条目的 12:00 / 18:00 是闹钟，另放 ``alarm``，不进 ``at``。
    """
    due = _item_due(item)
    if due is None or (due.date() - today).days > render.NEXT_DAYS:
        return None
    all_day = bool(item.get("all_day"))
    at = None if all_day or due.time() == time(0, 0) else due.time()
    return render.Entry(
        number=0,                     # 占位：图上号由 _board_numbers 决定
        title=str(item.get("title") or ""), at=at,
        all_day=all_day,
        alarm=_item_alarm(item, due) if at is None else None,
        list_name=name,
        overdue_days=max(0, (today - due.date()).days),
        due=due.date(),
        priority=prio.normalize(item.get("priority")),
        repeat=item.get("repeat") or None,
        ref=f"reminder:{item.get('id')}")


def _persist_board(ctx: Context, items, numbers: dict[str, int]) -> list[str]:
    """出图成功后写两处：整表重建镜像 ＋ 对齐图上号映射；失败只返回警告。"""
    warnings = _mirror_after_board(ctx, items)
    try:
        ctx.board.sync(numbers, day=ctx.today)     # 增量：老号不动、新号追加
    except Exception as exc:      # noqa: BLE001 —— 号映射写坏了只是 done 用不了
        warnings.append(f"看板编号映射写入失败：{exc}")
    return warnings


def _mirror_after_board(ctx: Context, items) -> list[str]:
    """按刚读到的 Reminder 状态整表重建镜像；失败只返回警告，绝不拖垮出图。"""
    rows = [_mirror_row(name, item) for name, item in items if item.get("id")]
    try:
        Mirror(ctx.inbox.db_path).replace_all(rows, now=ctx.now)
    except Exception as exc:      # noqa: BLE001 —— 镜像只是备份，board 才是主价值
        return [f"镜像重建失败：{exc}"]
    return []


def _mirror_row(name: str, item: dict) -> dict:
    """Reminder 条目 → ``Mirror.replace_all`` 认的行（列表名取循环里的，别信条目）。"""
    return {"reminder_id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "list_name": name,
            "due_at": item.get("dueDate") or None,
            "completed": int(bool(item.get("isCompleted"))),
            "priority": prio.normalize(item.get("priority")),
            "synced_at": None}       # 让 replace_all 用注入的 now 盖章


def _inbox_entries(ctx: Context) -> list:
    """收件箱念头 → 条目。顺序照库里的 created_at（排序在渲染层按优先级重排）。"""
    return [render.Entry(number=0, title=row["title"], at=None, list_name=INBOX_LIST_LABEL,
                         overdue_days=0, kind=INBOX_LIST_LABEL,
                         priority=prio.normalize(row.get("priority")),
                         ref=f"inbox:{row['id']}")
            for row in ctx.inbox.pending()]


# ---------------------------------------------------------------- priority

def cmd_priority(ctx: Context, args) -> Result:
    """改图上任何位置那一条的优先级：词汇与 remindctl 一致（none / low / medium / high）。

    号是**图上号**（``board_index``），收件箱念头与今天区 / 接下来区的任务一视同仁——
    老板看的是整张图，他说「6 号 high」时 6 号可能在哪一区都正常。
    """
    level = str(args.level).strip().lower()
    if level not in prio.PRIORITIES:
        return Result({"ok": False,
                       "error": f"优先级只认 {'/'.join(prio.PRIORITIES)}，收到「{args.level}」"},
                      code=EXIT_FAIL)
    try:
        number = int(args.number)
    except (TypeError, ValueError):
        return Result({"ok": False, "error": f"序号不是整数：{args.number}"}, code=EXIT_FAIL)
    result = _priority_one(ctx, number, level)
    return Result(result, code=EXIT_OK if result["ok"] else EXIT_FAIL)


def _priority_one(ctx: Context, number: int, level: str) -> dict:
    """按图上的号改一条的档位；号不对 / 那条不在了都返回 ok:False。"""
    ref, problem = _board_ref(ctx, number)
    if problem or ref is None:
        return {"ok": False, "number": number,
                "error": problem or f"今天没有 {number} 号"}
    kind, _, target = ref.partition(":")
    if kind == INBOX_LIST_LABEL:
        return _set_inbox_priority(ctx, number, ref, target, level)
    if kind == "reminder":
        return _set_reminder_priority(ctx, number, ref, target, level)
    return {"ok": False, "number": number, "ref": ref, "error": f"不认识的引用 {ref}"}


def _set_inbox_priority(ctx: Context, number: int, ref: str, target: str, level: str) -> dict:
    """收件箱念头：只改本地那一行（念头还没进 Reminder）。"""
    if not ctx.inbox.set_priority(int(target), level, now=ctx.now):
        return {"ok": False, "number": number, "ref": ref,
                "error": "这条念头已经迁出收件箱了（看图上的新号）"}
    return {"ok": True, "number": number, "ref": ref, "priority": level,
            "mark": prio.mark(level), "source": INBOX_LIST_LABEL}


def _set_reminder_priority(ctx: Context, number: int, ref: str, reminder_id: str,
                           level: str) -> dict:
    """今天区 / 接下来区的任务：改 Reminder 里那条，成功后同步本地镜像。

    已经是这个档位就原样返回 ``unchanged: true``——重复设同一档不该报错。
    """
    item = _reminder_item(ctx, reminder_id)
    if item is None:
        return {"ok": False, "number": number, "ref": ref,
                "error": f"{number} 号已经不在 Reminder 里了，重新看图"}
    if item.get("isCompleted"):
        return {"ok": False, "number": number, "ref": ref,
                "error": f"{number} 号已经完成了，重新看图"}
    previous = prio.normalize(item.get("priority"))
    if previous == level:
        return {"ok": True, "number": number, "ref": ref, "priority": level,
                "mark": prio.mark(level), "source": "reminder",
                "previous": previous, "unchanged": True}
    if not ctx.store.set_priority(reminder_id, level):
        return {"ok": False, "number": number, "ref": ref,
                "error": f"{number} 号的优先级没改成（Reminder 写失败）"}
    result = {"ok": True, "number": number, "ref": ref, "priority": level,
              "mark": prio.mark(level), "source": "reminder", "previous": previous}
    warnings = _sync_mirror_priority(ctx, reminder_id, level)
    return {**result, "warnings": warnings} if warnings else result


def _reminder_item(ctx: Context, reminder_id: str) -> dict | None:
    """按 id 找一条 Reminder（手机上完成 / 删掉的都会找不着）。"""
    for item in ctx.store.all():
        if str(item.get("id")) == reminder_id:
            return item
    return None


def _sync_mirror_priority(ctx: Context, reminder_id: str, level: str) -> list[str]:
    """镜像跟着改；镜像只是备份，失败进 warnings、不拖垮命令。"""
    try:
        Mirror(ctx.inbox.db_path).set_priority(reminder_id, level, now=ctx.now)
    except Exception as exc:      # noqa: BLE001 —— 镜像落后只是备份旧了
        return [f"镜像同步失败：{exc}"]
    return []


# ---------------------------------------------------------------- done

def cmd_done(ctx: Context, args) -> Result:
    """按**图上的号**完成；逐条报结果，有一条不行就算业务失败。

    号来自 ``board_index``（今天的图，当天不变）。今天还没出过图就报「先看一眼看板」，
    图上没有这个号就报「今天没有 N 号」——绝不猜、不退回去用别处的号。
    """
    results = [_done_one(ctx, raw) for raw in args.seqs]
    return Result({"ok": all(item["ok"] for item in results), "results": results},
                  code=EXIT_OK if all(item["ok"] for item in results) else EXIT_FAIL)


def _done_one(ctx: Context, raw) -> dict:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return {"seq": raw, "ok": False, "error": "序号不是整数"}
    ref, problem = _board_ref(ctx, number)
    if problem or ref is None:
        return {"seq": number, "ok": False, "error": problem or f"今天没有 {number} 号"}
    return _complete_ref(ctx, number, ref)


def _board_ref(ctx: Context, number: int) -> tuple[str | None, str | None]:
    """图上号 → (引用, 拒绝原因)。"""
    if ctx.board.count(day=ctx.today) == 0:
        return None, "今天还没出过图，先看一眼看板"
    ref = ctx.board.lookup(number, day=ctx.today)
    if ref is None:
        return None, f"今天没有 {number} 号"
    return ref, None


def _complete_ref(ctx: Context, number: int, ref: str) -> dict:
    kind, _, target = ref.partition(":")
    if kind == "reminder":
        gone = _reminder_problem(ctx, target)
        if gone:
            return {"seq": number, "ok": False, "ref": ref, "error": gone}
        ok = bool(ctx.store.complete(target))
        item = {"seq": number, "ok": ok, "ref": ref, "source": "reminder"}
        return item if ok else {**item, "error": "Reminder 完成失败"}
    if kind == INBOX_LIST_LABEL:          # 收件箱的号：done 就是「处理掉了」
        if ctx.inbox.mark_moved(target, "", now=ctx.now):
            return {"seq": number, "ok": True, "ref": ref, "source": INBOX_LIST_LABEL}
        return {"seq": number, "ok": False, "ref": ref, "source": INBOX_LIST_LABEL,
                "error": "这条念头已经迁出收件箱了（看图上的新号）"}
    return {"seq": number, "ok": False, "ref": ref, "error": f"不认识的引用 {ref}"}


def _reminder_problem(ctx: Context, reminder_id: str) -> str | None:
    """图上那条在 Reminder 里还在不在、是不是已经做掉了（手机上完成会这样）。"""
    for item in ctx.store.all():
        if str(item.get("id")) != reminder_id:
            continue
        if item.get("isCompleted"):
            return "这条已经完成了，重新看图"
        return None
    return "这条已经不在 Reminder 里了，重新看图"


# ---------------------------------------------------------------- list

def cmd_list(ctx: Context, _args) -> Result:
    """Reminder 各列表的条数（薄封装：只认 Protocol，不在这里起子进程）。"""
    totals: dict[str, int] = {}
    open_counts: dict[str, int] = {}
    for item in ctx.store.all():
        name = str(item.get("listName") or "?")
        totals[name] = totals.get(name, 0) + 1
        if not item.get("isCompleted"):
            open_counts[name] = open_counts.get(name, 0) + 1
    lists = [{"name": name, "total": totals[name], "open": open_counts.get(name, 0)}
             for name in sorted(totals)]
    return Result({"ok": True, "lists": lists})


# ---------------------------------------------------------------- doctor

def cmd_doctor(ctx: Context, _args) -> Result:
    """体检：只读，修不修由人定。problems 非空 = 有病，退出码 1。"""
    health = doctor.check(inbox_db=ctx.inbox.db_path, store=ctx.store, now=ctx.now)
    payload = {"ok": health.ok, "inbox_lag_days": health.inbox_lag_days,
               "inbox_count": health.inbox_count, "reminder_ok": health.reminder_ok,
               "mirror_drift": health.mirror_drift,
               "problems": list(health.problems)}
    return Result(payload, code=EXIT_OK if health.ok else EXIT_FAIL)


# ---------------------------------------------------------------- --human

def _human(command: str, payload: dict) -> str:
    if command == "doctor":
        return _human_doctor(payload)
    if not payload.get("ok", True):
        return f"✗ {payload.get('error') or '失败'}"
    if command in ("add", "clarify"):
        return (f"✓ #{payload['seq']} {payload.get('title') or ''}{_mark(payload)}"
                f"{_cycle(payload)}{_when(payload)}")
    if command == "priority":
        return _human_priority(payload)
    if command == "inbox":
        return _human_inbox(payload)
    if command == "board":
        return f"✓ {payload['count']} 条 → {payload['path']}"
    if command == "list":
        return _human_list(payload)
    if command == "done":
        return _human_done(payload)
    return json.dumps(payload, ensure_ascii=False)


def _mark(payload: dict) -> str:
    """回执里的优先级标记：有档位才印（high !!!、medium !!、low !）。"""
    mark = prio.mark(payload.get("priority"))
    return f" {mark}" if mark else ""


def _cycle(payload: dict) -> str:
    """回执里的周期：有重复才印（与视图同一个词，如「（每 5 天）」）。"""
    text = rep.label(payload.get("repeat"))
    return f"（{text}）" if text else ""


def _human_priority(payload: dict) -> str:
    if not payload.get("ok"):
        return f"✗ {payload.get('error') or '失败'}"
    mark = payload.get("mark") or "（无标记）"
    head = (f"✓ #{payload['number']} → {payload['priority']} {mark}"
            + ("（本来就是这档）" if payload.get("unchanged") else ""))
    warnings = payload.get("warnings") or []
    return "\n".join([head, *(f"· {item}" for item in warnings)])


def _when(payload: dict) -> str:
    """回执里的时间：具体时刻 / 上午 / 下午 / 全天（与图上同一个词）。"""
    if not payload.get("due"):
        return "（收件箱，还没定时间）"
    clock = payload.get("at") or SLOT_WORDS.get(str(payload.get("slot") or "")) or ALL_DAY_WORD
    return f" · {payload['due']} {clock}"


def _human_inbox(payload: dict) -> str:
    if not payload["items"]:
        return "收件箱是空的"
    lines = [f"{len(payload['items'])} 条待澄清 · 最老 {payload['oldest_days']} 天"]
    lines += [f"{item['id']}. {item['title']}（{item['age_days']} 天）"
              for item in payload["items"]]
    return "\n".join(lines)


def _display_width(text: str) -> int:
    """终端显示宽度：全角字符（汉字）占两格，中文列表名才对得齐。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _human_list(payload: dict) -> str:
    """``--human list``：每个列表一行「名字  总数 条（未完成数）」，不给 JSON。"""
    lists = payload.get("lists") or []
    if not lists:
        return "Reminder 里一个列表都没有"
    width = max(_display_width(row["name"]) for row in lists)
    return "\n".join(f"{row['name']}{' ' * (width - _display_width(row['name']) + 2)}"
                     f"{row['total']} 条（{row['open']} 未完成）" for row in lists)


def _human_done(payload: dict) -> str:
    if payload.get("results"):
        lines = [(f"✓ #{item['seq']} 完成" if item["ok"]
                  else f"✗ #{item['seq']} {item.get('error') or '失败'}")
                 for item in payload["results"]]
        return "\n".join(lines)
    return "✓ 完成"


def _human_doctor(payload: dict) -> str:
    if payload.get("error"):
        return f"✗ {payload['error']}"
    if payload.get("ok"):
        return (f"✓ 健康（收件箱 {payload['inbox_count']} 条 · "
                f"最老 {payload['inbox_lag_days']} 天 · 镜像差 {payload['mirror_drift']}）")
    lines = ["✗ 有问题：", *(f"· {item}" for item in payload.get("problems", []))]
    return "\n".join(lines)


# ---------------------------------------------------------------- 参数定义

DISPATCH = {"add": cmd_add, "inbox": cmd_inbox, "clarify": cmd_clarify,
            "priority": cmd_priority, "board": cmd_board, "view": cmd_view,
            "done": cmd_done, "list": cmd_list, "doctor": cmd_doctor}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtd", description="单人 GTD（v2）：有时间进 Reminder，没时间进收件箱")
    parser.add_argument("--version", action="version", version=f"gtd {__version__}")
    parser.add_argument("--db", metavar="PATH",
                        help="本地库路径；优先级 --db > GTD_DB > data/gtd.db")
    parser.add_argument("--now", metavar="WHEN",
                        help="钉住当前时刻（测试用），如 '2026-11-23 09:00'")
    parser.add_argument("--human", action="store_true", help="给人看的文字输出（默认 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)
    for builder in (_add, _inbox, _clarify, _priority, _board, _view, _done, _list, _doctor):
        builder(sub)
    return parser


def _add(sub) -> None:
    p = sub.add_parser("add", help="记一条：有时间进 Reminder，没时间进收件箱")
    p.add_argument("text")
    p.add_argument("--note", default="")
    p.add_argument("--priority", choices=prio.PRIORITIES, default=None,
                   help="手动指定优先级（覆盖从「很重要 / 急」里识别出来的那档）")


def _inbox(sub) -> None:
    sub.add_parser("inbox", help="列出还没迁出的念头")


def _clarify(sub) -> None:
    p = sub.add_parser("clarify", help="给收件箱里的念头补时间")
    p.add_argument("number", metavar="N", type=int,
                   help="当日序号（board / inbox 上的号）；没有序号时按 inbox 行 id")
    p.add_argument("text")


def _priority(sub) -> None:
    p = sub.add_parser("priority", help="设图上任何一条的优先级（词汇与 remindctl 一致）")
    p.add_argument("number", metavar="N", help="view / board 上的号（收件箱、今天、接下来都认）")
    p.add_argument("level", metavar="LEVEL", help="none | low | medium | high")


def _board(sub) -> None:
    p = sub.add_parser("board", help="出今日视图（一张 PNG）")
    p.add_argument("--out", metavar="PATH",
                   help="只写这一个路径；不给就落 out/board-YYYYMMDD-HHMMSS.png "
                        "＋ out/today-YYYYMMDD.png（当天最新副本）")


def _view(sub) -> None:
    p = sub.add_parser("view", help="全库文字视图：Markdown（默认）或 JSON，编号与 board 一致")
    p.add_argument("--format", choices=render.view_formats(), default="md",
                   help="md（默认，手机上直接看）/ json（脚本用）")


def _done(sub) -> None:
    p = sub.add_parser("done", help="按当日序号完成（可多个）")
    p.add_argument("seqs", nargs="+")


def _list(sub) -> None:
    sub.add_parser("list", help="Reminder 各列表条数")


def _doctor(sub) -> None:
    sub.add_parser("doctor", help="体检：收件箱积压 / Reminder 可用 / 镜像漂移")


if __name__ == "__main__":
    raise SystemExit(main())
