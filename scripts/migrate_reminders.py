#!/usr/bin/env python3
"""一次性迁移脚本：把一个旧 Reminder 列表（默认「提醒事项」）的条目搬进 GTD。

背景（老板 2026-11-23）：「这个是一次性的动作。相当于把旧有数据融合到我们现在的
体系当中去。」所以**不加 `gtd move` 子命令**，只留这一个可复用的脚本：

    uv run python scripts/migrate_reminders.py --dry-run
    uv run python scripts/migrate_reminders.py --apply --limit 1    # 先搬一条验证
    uv run python scripts/migrate_reminders.py --apply              # 全搬

规则：
- 默认只搬**未完成**条目（``--include-done`` 连已完成的也搬）
- 逐条 ``remindctl edit <id> --list <目标> --json``；任何一条失败**不中断**，
  继续搬后面的，最后统一报告「成功 N / 失败 M」（有失败退出码 1）
- 搬完打印源 / 目标列表的条数对照（``remindctl list --json`` 的 reminderCount）
- 不碰 ``board_index``：号由下次 ``gtd view`` / ``gtd board`` 分配

实测（2026-11-23，卡 t_eeb6c4ad）：「edit --list」只改列表，
``recurrenceRule`` 跟条目走 —— 搬一条 ``yearly`` 的到 GTD_TEST 再读回，
``{"frequency": "yearly", "interval": 1}`` 原样还在；搬回去同样保留。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gtd import priority as prio, repeat as rep                # noqa: E402
from gtd.reminders import (REMINDCTL_TIMEOUT, _localize,        # noqa: E402
                           _resolve_exe)

DEFAULT_SOURCE = "提醒事项"
DEFAULT_TARGET = "GTD"


# ---------------------------------------------------------------- remindctl 调用

def run_remindctl(args):
    """真实实现：跑一次 remindctl，返回 (rc, stdout, stderr)；异常一律折成失败。"""
    exe = _resolve_exe("remindctl")
    if not exe:
        return 127, "", "找不到 remindctl"
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              timeout=REMINDCTL_TIMEOUT)
    except Exception as exc:      # noqa: BLE001 —— 超时 / OSError 全兜
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _json_list(out):
    try:
        data = json.loads(out or "")
    except ValueError:
        return None
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else None


def fetch(name, *, include_done=False, run=run_remindctl):
    """读一个列表的条目（时间换本地、优先级与周期已归一）；读不出来返回 None。"""
    rc, out, err = run(["show", "all", "--list", name, "--json"])
    records = _json_list(out) if rc == 0 else None
    if records is None:
        return None
    items = [_localize(item) for item in records]
    return items if include_done else [i for i in items if not i.get("isCompleted")]


def counts(*, run=run_remindctl):
    """各列表的条数（remindctl 的 reminderCount = 未完成数）；拿不到返回 {}。"""
    rc, out, _err = run(["list", "--json"])
    records = _json_list(out) if rc == 0 else None
    if records is None:
        return {}
    return {str(row.get("title")): int(row.get("reminderCount") or 0) for row in records}


# ---------------------------------------------------------------- 展示

def when_of(item) -> str:
    """日期列：``YYYY-MM-DD HH:MM``；全天只给日期（有闹钟就补钟点）。"""
    due = str(item.get("dueDate") or "—")
    alarm = str(item.get("alarmDate") or "")
    if item.get("all_day") and alarm:
        return f"{due} {alarm.split(' ')[-1]}"
    return due


def describe(item) -> str:
    """dry-run 的一行：会搬：<标题> | <日期> | <优先级> | <重复规则>。"""
    return (f"会搬：{item.get('title') or ''} | {when_of(item)} | "
            f"{prio.normalize(item.get('priority'))} | "
            f"{rep.normalize(item.get('repeat')) or '无'}")


# ---------------------------------------------------------------- 两种模式

def dry_run(items, *, out=print) -> None:
    for item in items:
        out(describe(item))
    out(f"共 {len(items)} 条（--dry-run：一条都没动）")


def apply(items, target, *, run=run_remindctl, out=print) -> int:
    """逐条搬，失败不中断；返回失败条数。"""
    failed = 0
    for item in items:
        title = item.get("title") or ""
        rc, _stdout, err = run(["edit", str(item.get("id")), "--list", target, "--json"])
        if rc == 0:
            out(f"✓ 搬了：{title} → {target}")
        else:
            failed += 1
            reason = ((err or "").strip().splitlines() or [""])[0]
            out(f"✗ 失败：{title}（rc={rc}）{reason}")
    out(f"成功 {len(items) - failed} / 失败 {failed}")
    return failed


def _report_counts(names, before, after, *, out=print) -> None:
    for name in names:
        if name in before and name in after:
            out(f"{name}：{before[name]} 条 → {after[name]} 条")


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_reminders", description="一次性迁移：把旧 Reminder 列表的条目搬进 GTD")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只列会搬哪些，一条都不动")
    mode.add_argument("--apply", action="store_true", help="真搬")
    parser.add_argument("--from", dest="source", default=DEFAULT_SOURCE,
                        help=f"源列表名（默认 {DEFAULT_SOURCE}）")
    parser.add_argument("--to", dest="target", default=DEFAULT_TARGET,
                        help=f"目标列表名（默认 {DEFAULT_TARGET}）")
    parser.add_argument("--include-done", action="store_true",
                        help="连已完成的也搬（默认只搬未完成）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只搬前 N 条（先搬一条验证用；0 = 全搬）")
    return parser


def main(argv=None, *, run=run_remindctl, out=print) -> int:
    args = build_parser().parse_args(argv)
    items = fetch(args.source, include_done=args.include_done, run=run)
    if items is None:
        out(f"✗ 读列表「{args.source}」失败（列表不存在，或 remindctl 不可用）")
        return 1
    picked = items[:args.limit] if args.limit and args.limit > 0 else items
    if not picked:
        out(f"「{args.source}」里没有可搬的条目")
        return 0
    if args.dry_run:
        dry_run(picked, out=out)
        return 0
    before = counts(run=run)
    failed = apply(picked, args.target, run=run, out=out)
    _report_counts((args.source, args.target), before, counts(run=run), out=out)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
