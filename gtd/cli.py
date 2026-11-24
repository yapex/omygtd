"""命令层 v2：add / inbox / clarify / board / done / defer / list。

老板经对话让我调 CLI（不是自己敲），所以**默认输出 JSON**（机器读），
``--human`` 才给人看。退出码：0 成功 / 1 业务失败 / 2 参数错误。

依赖注入：
- ``store``：``ReminderStore`` 从外面传进来（测试传 ``FakeStore``），本模块不认识 remindctl
- ``now``：``--now "2026-11-23 09:00"`` 或 ``main(now=...)`` 钉死时钟（测试用）
- ``--db``：本地库路径（inbox / daily_seq / mirror），默认 ``data/gtd.db``，也认 ``GTD_DB``。
  **不是正式库就给 Reminder 开沙盒**：写入（add / clarify）一律落 ``GTD_TEST``，
  出图 / view / doctor 也只读 ``GTD_TEST``；列表不存在先自动 ``list --create``。

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
import traceback
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import (__version__, audit, doctor, lock, priority as prio, repeat as rep,
               render, timeutil)
from .inbox import DEFAULT_DB, PROJECT_ROOT, BoardIndex, Inbox, Mirror, Seq
from .reminders import TEST_LIST, RemindctlAdapter, ReminderStore

EXIT_OK, EXIT_FAIL, EXIT_PARAM = 0, 1, 2
LISTS = timeutil.LISTS
OUT_ENV = "GTD_OUT"
ALLOW_PROD_ENV = "GTD_ALLOW_PROD"           # 显式逃生门：worker 确实要动正式库时设 1
# worker 判据：看板卡进程的标记变量（dispatcher 注入）。实测 2026-11-23：
# worker 的 terminal 子 shell 里没有 HERMES_KANBAN_TASK，但 WORKSPACE / DB 在，
# 所以按「任一存在」判定，别只认一个名字。
WORKER_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
              "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB")
STAMP_FMT = "%Y-%m-%d %H:%M"
INBOX_LIST_LABEL = "inbox"
SLOT_WORDS = {"am": "上午", "pm": "下午"}      # --human 回执用的时段词
ALL_DAY_WORD = "全天"


@dataclass
class Result:
    payload: dict
    code: int = EXIT_OK
    audit: dict | None = None          # 审计材料（parsed / before / after），main 负责落盘


class ParamError(ValueError):
    """参数错（口语日期 / 非法钟点 / 旗标组合不对）→ 退出码 2，不是业务失败。"""


def _check_args(args) -> None:
    """写命令的严格入参校验：只认 ISO 日期与 24 小时钟点，口语一律拒。"""
    if args.command not in ("add", "defer", "clarify"):
        return
    _when_of(args)
    _check_repeat(args)


def _check_repeat(args) -> None:
    """``--repeat`` / ``--repeat-interval``：只有 add 有这两个旗标。"""
    if args.command != "add":
        return
    interval = args.repeat_interval
    if args.repeat is None:
        if interval != 1:
            raise ParamError("--repeat-interval 要和 --repeat 一起用"
                             "（例：--repeat weekly --repeat-interval 2）")
        return
    if interval < 1:
        raise ParamError(f"--repeat-interval 必须是正整数，收到「{interval}」"
                         "（例：--repeat weekly --repeat-interval 2）")


def _when_of(args) -> timeutil.When:
    """``--due`` / ``--at`` / ``--slot`` → When；非法组合抛 ParamError（文案带示例）。"""
    try:
        due = timeutil.parse_date(args.due) if args.due is not None else None
        at = timeutil.parse_clock(args.at) if args.at is not None else None
    except ValueError as exc:
        raise ParamError(str(exc)) from None
    slot = getattr(args, "slot", None)
    if at is not None and slot is not None:
        raise ParamError("--at 与 --slot 互斥：要具体钟点用 --at 09:30，"
                         "要上午/下午用 --slot am|pm（例：--due 2026-12-01 --at 09:30）")
    if due is None and (at is not None or slot is not None):
        raise ParamError("--at / --slot 需要 --due 一起给"
                         "（例：--due 2026-12-01 --at 09:30）")
    if due is None and args.command != "add":
        raise ParamError(f"{args.command} 必须给 --due YYYY-MM-DD（例：--due 2026-12-01）")
    if due is None and getattr(args, "repeat", None):
        raise ParamError("--repeat 需要 --due 指定锚点那一天"
                         "（例：--due 2026-11-30 --repeat weekly）")
    return timeutil.When(due=due, at=at, slot=slot)


def _spec(title: str, when: timeutil.When, level, cycle) -> dict:
    """审计与回执共用的入参口径（不再有「解析结果」这一层）。"""
    return {"title": title, **when.audit(),
            "priority": prio.normalize(level), "repeat": cycle}


def is_prod_db(db) -> bool:
    """这次写的是不是正式库 ``data/gtd.db``（其余路径一律当临时/测试库）。"""
    return Path(db or ":memory:").resolve() == Path(DEFAULT_DB).resolve()


def target_list(db) -> str:
    """写 Reminder 用哪张列表：**非正式库一律 GTD_TEST**（测试隔离），正式库写 GTD。

    老板 2026-11-23 定的规矩：临时库 / 测试库写来的任务不许混进真实 ``GTD``。
    列表不再从话里解析，所以只按 db 路径判（这里一处决定，add / clarify 都走它）。
    """
    if not is_prod_db(db):
        return TEST_LIST
    return timeutil.LIST_DEFAULT


@dataclass
class Context:
    """一次命令执行的上下文：本地库四张表 + Reminder 存储 + 钉住的时刻。"""

    inbox: Inbox
    seq: Seq
    board: BoardIndex
    store: ReminderStore
    now: datetime
    db: str = ""                       # 这次写的本地库；决定 Reminder 目标列表（见 test_db）

    @property
    def today(self) -> date:
        return self.now.date()

    @property
    def test_db(self) -> bool:
        """写的不是正式库 → 当测试库：Reminder 必须落 GTD_TEST（别污染老板的真实列表）。"""
        return not is_prod_db(self.db)

    @property
    def list_name(self) -> str:
        """这次写哪张 Reminder 列表（正式库 GTD，其它一律 GTD_TEST）。"""
        return target_list(self.db)


# ---------------------------------------------------------------- 入口

def main(argv=None, *, store: ReminderStore | None = None, now=None) -> int:
    """跑一条命令，返回退出码（0 成功 / 1 业务失败 / 2 参数错误）。"""
    raw = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = build_parser().parse_args(raw)
    except SystemExit as exc:          # argparse：未知命令、缺参数 → 2；--help → 0
        return int(exc.code or 0)
    try:
        _check_args(args)               # 严格入参：口语 / 非法值一律拒（退出码 2）
    except ParamError as exc:
        payload = {"ok": False, "error": str(exc)}
        _audit(args, raw, None, EXIT_PARAM, payload)
        _write(payload, args.human, args.command)
        return EXIT_PARAM
    try:
        _guard_prod_db(args)            # worker 默认连正式库 → 先拒，别等开了库才发现
        with _write_lock(args, raw):
            ctx = _context(args, store, now)
            result = DISPATCH[args.command](ctx, args)
    except lock.LockTimeout as exc:    # 另一进程正在写：等满 10 秒也不许硬闯
        payload = {"ok": False, "error": str(exc)}
        _audit(args, raw, None, EXIT_FAIL, payload)
        _write(payload, args.human, args.command)
        return EXIT_FAIL
    except (RuntimeError, ValueError, OSError) as exc:
        _audit(args, raw, None, EXIT_FAIL, {"ok": False, "error": str(exc)},
               tb=traceback.format_exc())
        _write({"ok": False, "error": str(exc)}, args.human, args.command)
        return EXIT_FAIL
    _audit(args, raw, result.audit, result.code, result.payload)
    _write(result.payload, args.human, args.command)
    return result.code


def _write_lock(args, raw):
    """只有写命令拿锁；view / board / list / doctor 只读，不该被别的进程阻塞。"""
    if args.command not in lock.WRITE_COMMANDS:
        return nullcontext()
    db = args.db or os.environ.get("GTD_DB") or DEFAULT_DB
    return lock.write_lock(db, tag=" ".join(raw))


def _in_kanban_worker() -> bool:
    """是不是看板卡的 worker 进程： ``HERMES_KANBAN_*`` 由 dispatcher 注入，人自己没有。"""
    return any(os.environ.get(name) for name in WORKER_ENV)


def _guard_prod_db(args) -> None:
    """禁止 worker 默认连正式库——它做端到端测试会把老板的真实数据写脏。

    2026-11-23 事故：卡 t_84767dcd 的 worker 跑 ``gtd clarify 7 --due 2026-12-01`` 没带
    ``--db``，直接澄清了老板真库里的一条念头。只拦**写**命令（``lock.WRITE_COMMANDS``）；
    ``view`` / ``board`` / ``list`` / ``doctor`` 对正式库只读，照旧放行。
    """
    if args.command not in lock.WRITE_COMMANDS or os.environ.get(ALLOW_PROD_ENV) == "1":
        return
    if not _in_kanban_worker():
        return                              # 老板手动跑，不受影响
    db = args.db or os.environ.get("GTD_DB") or DEFAULT_DB
    if not is_prod_db(db):
        return                              # --db / GTD_DB 指到别处：临时库，放行
    raise RuntimeError(
        f"Kanban worker 禁止连正式库 {DEFAULT_DB}（会污染真实数据）。"
        f"重跑请加 --db <临时路径>（或设 GTD_DB=<临时路径>）；"
        f"确实要动正式库时设 {ALLOW_PROD_ENV}=1 并说明理由。")


def _audit(args, raw, material, code, payload, *, tb=None) -> None:
    """把这条修改类命令写进 oplog；审计是旁路，失败也不影响退出码。"""
    if args.command not in audit.AUDITED:
        return
    audit.log(args.command, {
        "argv": raw, "db": args.db or os.environ.get("GTD_DB") or DEFAULT_DB,
        "now": _audit_now(args.now), "exit": code, "error": _error_of(payload),
        "traceback": _tb_lines(tb), **(material or {}),
    })


def _error_of(payload) -> str | None:
    """失败原因：顶层 error，或 done 逐条 error 拼一句（日志里要能一眼看到）。"""
    if not payload:
        return None
    if payload.get("error"):
        return str(payload["error"])
    problems = [f"#{item.get('seq')} {item.get('error')}"
                for item in payload.get("results") or () if item.get("error")]
    return "；".join(problems) or None


def _audit_now(raw) -> datetime | None:
    """``--now`` 注入的业务时刻；没注入就是 None（别让真实时间冒名顶替）。"""
    if raw is None:
        return None
    try:
        return _moment(raw)
    except ValueError:
        return None


def _tb_lines(tb) -> list[str] | None:
    """异常栈末 5 行（调试够用，不落整段）。"""
    return [line for line in str(tb or "").strip().splitlines() if line]


def _context(args, store, now) -> Context:
    moment = _moment(now if now is not None else args.now)
    db = args.db or os.environ.get("GTD_DB") or DEFAULT_DB
    return Context(Inbox(db), Seq(db), BoardIndex(db),
                   store if store is not None else RemindctlAdapter(), moment, str(db))


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


def _list_of(ctx: Context) -> str:
    """这次写哪张列表：跟着库走（临时库 → GTD_TEST），不看人记不记得。"""
    return target_list(ctx.db)


def _ensure_lists(ctx: Context) -> None:
    """写之前确保目标列表存在：正式库建 GTD / Waiting，临时库只建 GTD_TEST。

    ``remindctl list <name> --create`` 是幂等的（已存在时不动里面的条目），
    所以新机器 / 列表被删了也能自愈。
    """
    if not ctx.test_db:
        ctx.store.ensure_lists()
        return
    _ensure_one(ctx.store, TEST_LIST)


def _ensure_one(store: ReminderStore, name: str) -> None:
    """确保单张列表存在；老鸭子实现没有 ``ensure_list`` 就退回全量 ``ensure_lists``。"""
    ensure = getattr(store, "ensure_list", None)
    if ensure is None:
        store.ensure_lists()
        return
    ensure(name)


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


# ---------------------------------------------------------------- 审计材料

AUDIT_KEYS = ("source", "seq", "inbox_id", "reminder_id", "number", "ref", "priority",
              "previous", "unchanged", "due", "at", "all_day", "slot", "results")


def _audited(result: Result, spec=None, before=None, after=None) -> Result:
    """给回执挂上审计三问（入参口径 / before / after）；after 不给就取回执里的状态。"""
    result.audit = {
        "parsed": spec,
        "before": before,
        "after": after if after is not None else _after(result.payload),
    }
    return result


def _after(payload: dict) -> dict:
    """回执里的状态字段 → 「改之后」快照（不搬渲染文本）。"""
    return {key: payload[key] for key in AUDIT_KEYS if key in payload}


def _inbox_snapshot(row: dict | None) -> dict | None:
    """inbox 一行 → 审计快照：moved_at / reminder_id 就是「改了什么」的证据。"""
    if row is None:
        return None
    return {"inbox_id": row["id"], "moved_at": row["moved_at"],
            "reminder_id": row["reminder_id"],
            "priority": prio.normalize(row.get("priority"))}


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
    """严格入参：时间只认 --due / --at / --slot；没给 --due 就进收件箱。

    标题**原样入库**，不做任何词剥离（「明天买菜」就是一条叫「明天买菜」的念头）。
    给了 --due：只日期 → 全天无闹钟；--slot → 全天 + 12:00/18:00 闹钟；
    --at → due 就是那一刻（到点响）。--repeat 的锚点就是 --due 那天。
    """
    when = _when_of(args)
    level = prio.normalize(args.priority)
    cycle = rep.spec(args.repeat, args.repeat_interval) if args.repeat else None
    spec = _spec(args.text, when, level, cycle)
    if when.due is None:
        return _audited(_park(ctx, args.text, when, args.note,
                              level=level, cycle=cycle), spec)
    _ensure_lists(ctx)
    reminder_id = ctx.store.add(args.text, when.moment, _list_of(ctx),
                                advance_minutes=when.advance_minutes, note=args.note,
                                all_day=when.all_day, slot=when.slot,
                                priority=level, repeat=cycle)
    if not reminder_id:
        return _audited(_park(ctx, args.text, when, args.note, level=level, cycle=cycle,
                              error="Reminder 写不进去，已留在收件箱"), spec)
    return _audited(Result(_landed(ctx, args.text, reminder_id, when, level, cycle)), spec)


def _landed(ctx: Context, title: str, reminder_id: str, when: timeutil.When,
            level=None, cycle=None) -> dict:
    """写成功后的回执：``at`` 只报具体时刻，全天/时段另给标记（别把 00:00 当时刻）。"""
    return {"ok": True, "seq": _seq_of(ctx, f"reminder:{reminder_id}"),
            "title": title, "list": _list_of(ctx),
            "due": when.due.isoformat(),
            "at": _hhmm(when.moment) if when.at is not None else None,
            "all_day": when.all_day, "slot": when.slot,
            "priority": prio.normalize(level), "repeat": cycle,
            "reminder_id": reminder_id, "source": "reminder"}


def _park(ctx: Context, title: str, when: timeutil.When, note,
          error: str | None = None, level=None, cycle=None) -> Result:
    """念头落 inbox（不丢），迁出标记留给 clarify / done。"""
    level = prio.normalize(level)
    inbox_id = ctx.inbox.add(title, note or "", now=ctx.now, priority=level)
    payload = {"ok": error is None, "seq": _seq_of(ctx, f"inbox:{inbox_id}"),
               "title": title, "list": None, "source": "inbox",
               "due": when.due.isoformat() if when.due else None,
               "at": _hhmm(when.moment) if when.at is not None else None,
               "all_day": when.all_day, "slot": when.slot,
               "priority": level, "repeat": cycle, "inbox_id": inbox_id}
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
    """给 inbox 里的念头补时间：Reminder 成功才打迁出标记。

    时间由调用方换算好，用 --due / --at / --slot 传进来；标题沿用 inbox 那一行。
    """
    row = _pending_row(ctx, args.number)
    if row is None:
        return _audited(Result({"ok": False,
                                "error": f"收件箱里没有 #{args.number}"
                                         f"（可能已定时间，或者号不存在）"},
                               code=EXIT_FAIL), before={"number": args.number})
    when = _when_of(args)
    before = _inbox_snapshot(row)
    title, level = row["title"], prio.normalize(row.get("priority"))
    spec = _spec(title, when, level, None)
    _ensure_lists(ctx)
    reminder_id = ctx.store.add(title, when.moment, _list_of(ctx),
                                advance_minutes=when.advance_minutes,
                                note=row["note"], all_day=when.all_day,
                                slot=when.slot, priority=level, repeat=None)
    if not reminder_id:
        return _audited(Result({"ok": False, "error": "Reminder 写不进去，念头留在收件箱",
                                "inbox_id": row["id"]}, code=EXIT_FAIL), spec, before)
    ctx.inbox.mark_moved(row["id"], reminder_id, now=ctx.now)
    payload = _landed(ctx, title, reminder_id, when, level, None)
    payload["inbox_id"] = row["id"]
    after = _inbox_snapshot(ctx.inbox.get(row["id"]))
    return _audited(Result(payload), spec, before, after=after)


def _pending_row(ctx: Context, number: int) -> dict | None:
    """按「老板看到的号」找：先看当日序号（board / inbox 上印的号），再退回 inbox 行 id。

    日常路径是序号：老板在图上看到 3，就说「3 ...」。行 id 是兜底：
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
    会出现的条目（``number`` 已按 ``board_index`` 填好）；``order`` 是图上从上到下
    **真画出来**的顺序（今日 → 接下来 → 收件箱，「更远」只折一行不逐条画），
    ``numbers`` 是含「更远」在内每条的号（文字视图要全量印号）。
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
    order = render.board_order(entries, now=ctx.now)      # 图上真画出来的顺序
    far = render.far_entries(entries, now=ctx.now)        # 「更远」：图上折一行，文字全量
    numbers = _board_numbers(ctx, order + far)           # 引用 → 当天固定的图上号
    numbered = [replace(entry, number=numbers.get(entry.ref, entry.number))
                for entry in entries]     # 「更远」的也带上号：文字视图要全量印
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
    """``GTD`` / ``Waiting`` 里未完成的全部条目（含明天以后）：镜像要全量。

    临时库（测试库）只读 ``GTD_TEST``：写进去的测试条目本来就落那儿，读侧跟着走，
    「临时库 add 完再 view」才能看见自己刚加的那条。正式库不受影响。
    """
    names = (TEST_LIST,) if ctx.test_db else LISTS
    return [(name, item) for name in names for item in ctx.store.pending(name)]


def _today_tasks(ctx: Context, items) -> list:
    """Reminder 未完成条目 → 渲染层条目（不按 7 天窗口丢：更远的要进「更远」区）。

    未来条目也带上 ``due``：渲染层靠它把「今日」「接下来」「更远」分开，并印出
    「周五 15:30」/「2027-03-01」这样的日期。窗口宽度取 ``render.NEXT_DAYS``，
    只在渲染层定义一次。每条都带 ``ref``：图上号就靠它跟 ``board_index`` 对上。
    """
    return [entry for entry in (_reminder_entry(name, item, ctx.today)
                                for name, item in items) if entry is not None]


def _reminder_entry(name: str, item: dict, today: date):
    """一条 Reminder → 视图条目；只有**没有 due** 的返回 None。

    超过 7 天的**不再丢**（老板 2026-11-23：年度任务搬进 GTD 后视图上看不到）：
    图上折进「更远」一行，文字视图全量列出。全天条目 ``at=None`` + ``all_day=True``：
    **不许把 00:00 当时刻**（EventKit 存的全天就是本地零点）。时段条目的 12:00 / 18:00
    是闹钟，另放 ``alarm``，不进 ``at``。
    """
    due = _item_due(item)
    if due is None:
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
        return _audited(Result({"ok": False,
                                "error": f"优先级只认 {'/'.join(prio.PRIORITIES)}，"
                                         f"收到「{args.level}」"}, code=EXIT_FAIL),
                        before={"level": args.level})
    try:
        number = int(args.number)
    except (TypeError, ValueError):
        return _audited(Result({"ok": False, "error": f"序号不是整数：{args.number}"},
                               code=EXIT_FAIL), before={"number": args.number})
    before = _priority_snapshot(ctx, number)
    result = _priority_one(ctx, number, level)
    return _audited(Result(result, code=EXIT_OK if result["ok"] else EXIT_FAIL),
                    before=before, after=result)


def _priority_snapshot(ctx: Context, number: int) -> dict:
    """改之前那条的档位（号不对就带上原因）；priority 的 before 靠它。"""
    ref, problem = _board_ref(ctx, number)
    if problem or ref is None:
        return {"number": number, "error": problem}
    kind, _, target = ref.partition(":")
    if kind == INBOX_LIST_LABEL:
        row = ctx.inbox.get(int(target))
        current = prio.normalize(row.get("priority")) if row else None
    else:
        item = _reminder_item(ctx, target)
        current = prio.normalize(item.get("priority")) if item else None
    return {"number": number, "ref": ref, "priority": current}


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
    before = _done_snapshot(ctx, args.seqs)      # 先拍快照，拍完再动手
    results = [_done_one(ctx, raw) for raw in args.seqs]
    ok = all(item["ok"] for item in results)
    return _audited(Result({"ok": ok, "results": results},
                           code=EXIT_OK if ok else EXIT_FAIL),
                    before=before, after=results)


def _done_snapshot(ctx: Context, raws) -> list[dict]:
    """完成之前每条的状态：号 → 引用 → 已完成 / 收件箱是否已迁出。"""
    snapshots = []
    for raw in raws:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            snapshots.append({"seq": raw, "error": "序号不是整数"})
            continue
        ref, problem = _board_ref(ctx, number)
        snapshots.append(_done_state(ctx, number, ref, problem))
    return snapshots


def _done_state(ctx: Context, number: int, ref: str | None, problem: str | None) -> dict:
    """一条 done 的 before 快照（号不对就只留原因）。"""
    if problem or ref is None:
        return {"seq": number, "error": problem}
    kind, _, target = ref.partition(":")
    if kind == INBOX_LIST_LABEL:
        row = ctx.inbox.get(int(target))
        return {"seq": number, "ref": ref, "moved": bool(row and row["moved_at"])}
    item = _reminder_item(ctx, target)
    return {"seq": number, "ref": ref,
            "completed": bool(item.get("isCompleted")) if item else None}


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


def _repeat_problem(ctx: Context, reminder_id: str) -> str | None:
    """重复任务不许 ``defer``：EventKit 的周期规则以 dueDate 为锚点滚动。

    实测（2026-11-24，GTD_TEST，weekly/1 那条）：``edit --due`` 之后
    ``recurrenceRule`` 本身不变（frequency / interval 都在），但 ``complete``
    往前滚的是**改后的 due**——周一的任务 defer 到周三，滚出来是下周三，
    原周一锚点就丢了。所以这里直接拒：要改节奏去 Reminders.app 里改频率。
    """
    item = _reminder_item(ctx, reminder_id)
    if item is None:
        return None
    if item.get("recurrenceRule") or item.get("repeat"):
        return "重复任务暂不支持 defer（改期会把重复节奏的锚点一起挪走），请改 Reminder 里的频率"
    return None


# ---------------------------------------------------------------- defer

def cmd_defer(ctx: Context, args) -> Result:
    """按**图上的号**改到期时间：改期，不是完成，也不重建条目。

    时间由调用方换算好，用 --due / --at / --slot 传进来（与 add 同一套语义）：
    只给日期 → 全天、无闹钟；--slot → 全天 + 12:00/18:00 闹钟；--at → due 就是那一刻。
    多号一次改完。

    号来自 ``board_index``，规则与 done 一致（没出图 / 没这个号 / 已完成 都拒）。
    引用不变，所以**图上号也不变**：同一条只是换了日子，不重新编号。
    镜像那一行的 due 跟着改（可整表重建的备份，别让本地副本落后）。
    带 ``recurrenceRule`` 的重复任务直接拒（见 ``_repeat_problem``）：EventKit
    的周期以 due 为锚点，改这一条等于把整条链的节奏一起挪走（2026-11-24 实测）。
    """
    when = _when_of(args)
    before = _defer_snapshot(ctx, args.seqs)
    results = [_defer_one(ctx, raw, when) for raw in args.seqs]
    ok = all(item["ok"] for item in results)
    return _audited(Result({"ok": ok, "results": results},
                           code=EXIT_OK if ok else EXIT_FAIL),
                    _spec("", when, None, None), before, after=results)


def _defer_one(ctx: Context, raw, when: timeutil.When) -> dict:
    """改一条的到期时间；号不对 / 已完成 / 还在收件箱都返回 ok:False。"""
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return {"seq": raw, "ok": False, "error": "序号不是整数"}
    ref, problem = _board_ref(ctx, number)
    if problem or ref is None:
        return {"seq": number, "ok": False, "error": problem or f"今天没有 {number} 号"}
    kind, _, target = ref.partition(":")
    if kind == INBOX_LIST_LABEL:      # 念头还没进 Reminder，没有到期时间可改
        return {"seq": number, "ok": False, "ref": ref, "source": INBOX_LIST_LABEL,
                "error": f"{number} 号还在收件箱里（没有到期时间）；"
                         f"用 gtd clarify {number} --due {when.due.isoformat()} 定时间"}
    if kind != "reminder":
        return {"seq": number, "ok": False, "ref": ref, "error": f"不认识的引用 {ref}"}
    gone = _reminder_problem(ctx, target)
    if gone:
        return {"seq": number, "ok": False, "ref": ref, "error": gone}
    repeated = _repeat_problem(ctx, target)      # 重复任务改期会挪走锚点，拒
    if repeated:
        return {"seq": number, "ok": False, "ref": ref, "source": "reminder",
                "error": repeated}
    changed = ctx.store.reschedule(target, when.moment,
                                   advance_minutes=when.advance_minutes,
                                   all_day=when.all_day, slot=when.slot)
    if not changed:
        return {"seq": number, "ok": False, "ref": ref, "source": "reminder",
                "error": f"{number} 号的到期时间没改成（Reminder 写失败）"}
    item = {"seq": number, "ok": True, "ref": ref, "source": "reminder",
            "due": when.due.isoformat(),
            "at": _hhmm(when.moment) if when.at is not None else None,
            "all_day": when.all_day, "slot": when.slot, "reminder_id": target}
    warnings = _sync_mirror_due(ctx, target)
    return {**item, "warnings": warnings} if warnings else item


def _defer_snapshot(ctx: Context, raws) -> list[dict]:
    """改期之前每条的状态：号 → 引用 → 原来的 due / 闹钟（审计的 before）。"""
    snapshots = []
    for raw in raws:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            snapshots.append({"seq": raw, "error": "序号不是整数"})
            continue
        ref, problem = _board_ref(ctx, number)
        snapshots.append(_defer_state(ctx, number, ref, problem))
    return snapshots


def _defer_state(ctx: Context, number: int, ref: str | None, problem: str | None) -> dict:
    """一条 defer 的 before 快照（号不对就只留原因）。"""
    if problem or ref is None:
        return {"seq": number, "error": problem}
    kind, _, target = ref.partition(":")
    item = _reminder_item(ctx, target) if kind == "reminder" else None
    return {"seq": number, "ref": ref, "due": (item or {}).get("dueDate"),
            "alarm": (item or {}).get("alarmDate")}


def _sync_mirror_due(ctx: Context, reminder_id: str) -> list[str]:
    """镜像那一行的 due 跟着改（读改后的真值）；失败只进 warnings。"""
    item = _reminder_item(ctx, reminder_id)
    try:
        if item is not None:
            Mirror(ctx.inbox.db_path).set_due(reminder_id, item.get("dueDate"), now=ctx.now)
    except Exception as exc:      # noqa: BLE001 —— 镜像落后只是备份旧了
        return [f"镜像同步失败：{exc}"]
    return []


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
    health = doctor.check(inbox_db=ctx.inbox.db_path, store=ctx.store, now=ctx.now,
                          lists=(TEST_LIST,) if ctx.test_db else doctor.DEFAULT_LISTS)
    payload = {"ok": health.ok, "inbox_lag_days": health.inbox_lag_days,
               "inbox_count": health.inbox_count, "reminder_ok": health.reminder_ok,
               "mirror_drift": health.mirror_drift,
               "problems": list(health.problems)}
    return Result(payload, code=EXIT_OK if health.ok else EXIT_FAIL)


# ---------------------------------------------------------------- --human

def _human(command: str, payload: dict) -> str:
    if command == "doctor":
        return _human_doctor(payload)
    if command == "defer":
        return _human_defer(payload)
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


def _human_defer(payload: dict) -> str:
    """``--human defer``：一行一条「✓ #3 改到 2026-11-24 上午」。"""
    results = payload.get("results") or []
    if not results:
        return f"✗ {payload.get('error') or '失败'}"
    return "\n".join(_human_defer_line(item) for item in results)


def _human_defer_line(item: dict) -> str:
    """单条改期回执：失败印原因，成功印「改到哪天哪一段」。"""
    if not item.get("ok"):
        return f"✗ #{item.get('seq')} {item.get('error') or '失败'}"
    clock = item.get("at") or SLOT_WORDS.get(str(item.get("slot") or "")) or ALL_DAY_WORD
    return f"✓ #{item['seq']} 改到 {item.get('due')} {clock}"


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
            "done": cmd_done, "defer": cmd_defer, "list": cmd_list,
            "doctor": cmd_doctor}


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
    for builder in (_add, _inbox, _clarify, _priority, _board, _view, _done, _defer,
                    _list, _doctor):
        builder(sub)
    return parser


def _add(sub) -> None:
    p = sub.add_parser("add", help="记一条：有时间进 Reminder，没时间进收件箱")
    p.add_argument("text", help="标题（原样入库，不做词剥离）")
    p.add_argument("--due", metavar="YYYY-MM-DD",
                   help="到期日；不给就进收件箱（例：2026-12-01）")
    _when_flags(p)
    p.add_argument("--repeat", choices=rep.FLAGS,
                   help="重复频率；锚点就是 --due 那天（daily / weekly / monthly）")
    p.add_argument("--repeat-interval", type=int, default=1, metavar="N",
                   help="每隔几个周期一次（默认 1；例：--repeat weekly --repeat-interval 2）")
    p.add_argument("--priority", choices=prio.PRIORITIES, default=prio.PRIORITY_NONE,
                   help="优先级 none/low/medium/high（默认 none）")
    p.add_argument("--note", default="")


def _when_flags(p) -> None:
    """三个时间旗标在 add / defer / clarify 上完全同一套。"""
    p.add_argument("--at", metavar="HH:MM",
                   help="具体钟点，24 小时制（例：09:30）；与 --slot 互斥")
    p.add_argument("--slot", choices=timeutil.SLOTS,
                   help="时段 am|pm：due 全天 + 12:00/18:00 闹钟")


def _inbox(sub) -> None:
    sub.add_parser("inbox", help="列出还没迁出的念头")


def _clarify(sub) -> None:
    p = sub.add_parser("clarify", help="给收件箱里的念头补时间")
    p.add_argument("number", metavar="N", type=int,
                   help="当日序号（board / inbox 上的号）；没有序号时按 inbox 行 id")
    p.add_argument("--due", metavar="YYYY-MM-DD", help="到期日（必填，例：2026-12-01）")
    _when_flags(p)


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


def _defer(sub) -> None:
    p = sub.add_parser("defer", help="按当日序号改到期时间（改期，不是完成；可多个）")
    p.add_argument("seqs", nargs="+", metavar="N...", help="图上号，可多个")
    p.add_argument("--due", metavar="YYYY-MM-DD", help="新到期日（必填，例：2026-12-01）")
    _when_flags(p)


def _list(sub) -> None:
    sub.add_parser("list", help="Reminder 各列表条数")


def _doctor(sub) -> None:
    sub.add_parser("doctor", help="体检：收件箱积压 / Reminder 可用 / 镜像漂移")


if __name__ == "__main__":
    raise SystemExit(main())
