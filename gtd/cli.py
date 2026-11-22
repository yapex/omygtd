"""命令层：参数解析与调度，默认输出 JSON。

职责边界（S/D）：只解析参数、调数据层、把产物交给输出。
- 不 import sqlite3，SQL 全在 store.py
- 不按格式分支：渲染器一律从注册表取（L）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from . import render, store as gstore

EXIT_OK, EXIT_PARAM, EXIT_NOT_FOUND = 0, 1, 2
EDIT_FIELDS = ("title", "due", "slot", "category", "note", "waiting_on", "defer")


@dataclass
class Result:
    payload: dict
    code: int = EXIT_OK
    text: str | None = None


class _Parser(argparse.ArgumentParser):
    """参数错统一走 ParamError（退出码 1），不让 argparse 自己 exit(2)。"""

    def error(self, message):
        raise gstore.ParamError(message)


# ---------------------------------------------------------------- 调度

def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except gstore.ParamError as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_PARAM
    store = None
    try:
        store = gstore.Store(getattr(args, "db", None))
        store.init()
        result = DISPATCH[args.command](store, args)
    except gstore.NotFoundError as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_NOT_FOUND
    except gstore.WriteError as exc:
        _emit({"ok": False, "error": str(exc), "capture_id": exc.capture_id,
               "raw_kept": True})
        return EXIT_NOT_FOUND
    except (gstore.ParamError, gstore.ConstraintError, ValueError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return EXIT_PARAM
    finally:
        if store is not None:
            store.close()
    _emit(result.payload, result.text)
    return result.code


def _emit(payload: dict, text: str | None = None) -> None:
    if text is not None:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- 写命令

def cmd_add(store, args) -> Result:
    return Result(store.add(args.title, due=args.due, slot=args.slot,
                            category=args.category, waiting_on=args.waiting_on,
                            defer=args.defer, note=args.note, dup_ok=args.dup_ok))


def cmd_clarify(store, args) -> Result:
    return Result(store.clarify(args.id, args.action, due=args.due, slot=args.slot,
                                category=args.category, note=args.note, day=args.day))


def cmd_done(store, args) -> Result:
    return Result(store.complete(args.ids, day=args.day))


def cmd_drop(store, args) -> Result:
    return Result(store.drop(args.ids, day=args.day))


def cmd_edit(store, args) -> Result:
    fields = {key: getattr(args, key) for key in EDIT_FIELDS
              if getattr(args, key, None) is not None}
    return Result(store.edit(args.id, day=args.day, **fields))


def cmd_undo(store, _args) -> Result:
    return Result(store.undo())


# ---------------------------------------------------------------- 读命令

def cmd_show(store, args) -> Result:
    row = store.require(args.id, args.day)
    return Result({"ok": True, "seq": store.seq_of(row["id"], args.day),
                   "task": store.public(row)})


def cmd_board(store, args) -> Result:
    day = args.day or gstore.today_str()
    stream = store.entries(day)
    renderer = render.get_renderer(args.format)
    artifact = renderer.render(stream, args.view, _board_ctx(store, day, args))
    payload = {"ok": True, "view": args.view, "format": args.format, "day": day,
               "count": len(render.select_view(stream, args.view, day))}
    if artifact.text is not None:
        return Result(payload, text=artifact.text)
    return Result({**payload, "path": str(artifact.path)})


def _board_ctx(store, day: str, args) -> dict:
    out = Path(args.out).expanduser() if args.out else _default_png(args.view, day)
    return {"day": day, "generated_at": gstore.now_iso(), "stats": store.stats(),
            "out": out, "db": str(store.db_path),
            "category_order": list(gstore.CATEGORIES)}


def _default_png(view: str, day: str) -> Path:
    root = Path(os.environ.get("GTD_OUT") or gstore.PROJECT_ROOT / "out")
    return root / f"{view}-{day.replace('-', '')}.png"


def cmd_init(store, _args) -> Result:
    return Result(store.init())


def cmd_sync(_store, _args) -> Result:
    return Result({"ok": True, "implemented": False, "message": "二期未实现"})


# ---------------------------------------------------------------- 运维命令

def cmd_doctor(store, _args) -> Result:
    return Result(store.doctor())


def cmd_backup(store, _args) -> Result:
    report = store.backup()
    if report.get("performed"):
        report["archived"] = store.archive_old_done()
        report["snapshot"] = _write_snapshot(store)
    return Result(report)


def _write_snapshot(store) -> str:
    day = gstore.today_str()
    path = Path(store.db_path).parent / "snapshots" / f"{day.replace('-', '')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact = render.get_renderer("md").render(store.entries(day), "all", {"day": day})
    path.write_text(artifact.text or "", encoding="utf-8")
    return str(path)


DISPATCH = {
    "add": cmd_add, "clarify": cmd_clarify, "done": cmd_done, "drop": cmd_drop,
    "edit": cmd_edit, "show": cmd_show, "board": cmd_board, "undo": cmd_undo,
    "doctor": cmd_doctor, "backup": cmd_backup, "init": cmd_init, "sync": cmd_sync,
}


# ---------------------------------------------------------------- 参数定义

def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="gtd", description="单人 GTD 系统（MVP-1，本地 SQLite）")
    parser.add_argument("--version", action="version", version=f"gtd {__version__}")
    parser.add_argument("--db", metavar="PATH",
                        help="库文件路径；优先级 --db > GTD_DB > 默认正式库")
    sub = parser.add_subparsers(dest="command", required=True)
    for builder in (_add, _clarify, _done, _drop, _edit, _show, _board,
                    _undo, _doctor, _backup, _init, _sync):
        builder(sub)
    return parser


def _add(sub):
    p = sub.add_parser("add", help="捕获一条任务")
    p.add_argument("title")
    p.add_argument("--due")
    p.add_argument("--slot")
    p.add_argument("--category", choices=gstore.CATEGORIES)
    p.add_argument("--waiting-on", dest="waiting_on")
    p.add_argument("--defer")
    p.add_argument("--note")
    p.add_argument("--dup-ok", dest="dup_ok", action="store_true")


def _clarify(sub):
    p = sub.add_parser("clarify", help="收件箱 → 具体下一步")
    p.add_argument("id")
    p.add_argument("--action", required=True)
    p.add_argument("--due")
    p.add_argument("--slot")
    p.add_argument("--category", choices=gstore.CATEGORIES)
    p.add_argument("--note")
    p.add_argument("--day")


def _done(sub):
    p = sub.add_parser("done", help="完成（可多个 id）")
    p.add_argument("ids", nargs="+")
    p.add_argument("--day")


def _drop(sub):
    p = sub.add_parser("drop", help="放弃（可多个 id）")
    p.add_argument("ids", nargs="+")
    p.add_argument("--day")


def _edit(sub):
    p = sub.add_parser("edit", help="改内容字段（不能改 status）")
    p.add_argument("id")
    for flag in ("--title", "--due", "--slot", "--category", "--note",
                 "--waiting-on", "--defer"):
        p.add_argument(flag)
    p.add_argument("--day")


def _show(sub):
    p = sub.add_parser("show", help="显示单条")
    p.add_argument("id")
    p.add_argument("--day")


def _board(sub):
    p = sub.add_parser("board", help="看板：today / inbox / categories / all")
    p.add_argument("--view", choices=render.VIEWS, default="today")
    p.add_argument("--format", choices=render.formats(), default="json")
    p.add_argument("--out")
    p.add_argument("--day")


def _undo(sub):
    sub.add_parser("undo", help="撤销上一次写操作")


def _doctor(sub):
    sub.add_parser("doctor", help="体检")


def _backup(sub):
    sub.add_parser("backup", help="当日有变化则备份")


def _init(sub):
    sub.add_parser("init", help="建库建目录")


def _sync(sub):
    sub.add_parser("sync", help="二期桩")


if __name__ == "__main__":
    raise SystemExit(main())
