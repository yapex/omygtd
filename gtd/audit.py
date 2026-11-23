"""调试期操作日志：每次修改类命令往 ``oplog.jsonl`` 写一行 JSON。

老板的原话是「这段时间功能刚开发完，不一定稳定，把日志加进去方便调试」，所以每条
记的是**调试三问**：这条命令做了什么（``argv`` + ``parsed``）、改前改后是什么
（``before`` / ``after``）、失败为什么（``error`` + ``traceback`` 末 5 行）。

一条硬规矩：**``at`` 与 ``now`` 必须分开**。
``at`` 是真实墙钟（这一行实际写下的那一刻）；``now`` 是 ``--now`` 注入的**业务时间**
（没注入就是 null）。2026-11-23 那次事故就是两者混在一起——``inbox.moved_at`` 写着
16:00（来自 ``--now 16:00``），而 Reminder 是 16:02 才建的，矛盾查了半天。

为什么用标准库 ``logging`` 而不是自己 ``open(..., "a")``：进程内锁、滚动、级别、
异常兜底都是现成的。业务代码只调 ``audit.log(cmd, payload)``，不碰 ``logging``。
写日志**绝不影响主流程**：``log()`` 内部全包 try/except，失败只在 stderr 提一句。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "gtd.audit"
FILENAME = "oplog.jsonl"
ENV_PATH = "GTD_LOG"                   # 覆盖日志落点（测试用；正式跑不设）
ENV_SESSION = "HERMES_SESSION_ID"
AUDITED = ("add", "clarify", "done", "priority")
MAX_BYTES = 5 * 1024 * 1024            # 单文件 5 MB
BACKUP_COUNT = 1                       # 只留一代 oplog.jsonl.1
KEEP_TEXT = 200                        # 长文本（将来的 --note）截断，别把日志撑爆
KEEP_TB_LINES = 5                      # 失败时 traceback 只留末 5 行

logging.raiseExceptions = False        # handler 自己出错也不许把异常抛回业务


class _JsonFormatter(logging.Formatter):
    """一条日志 = 一行 JSON（payload 里的 dict 原样铺开，不套消息体）。"""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(getattr(record, "payload", {}), ensure_ascii=False)


def wall_clock() -> datetime:
    """真实墙钟（审计专用）；测试 monkeypatch 它来确认没读 ``--now``。"""
    return datetime.now().astimezone()


def path_for(db_path) -> Path | None:
    """日志落点：``GTD_LOG`` > 库文件同目录的 ``oplog.jsonl``；内存库不写。

    跟库走而不是写死项目 ``data/``：``--db`` 指到 ``tmp_path`` 时日志也落临时目录，
    测试一条都不会灌进正式日志；正式库 ``data/gtd.db`` 自然就落在 ``data/`` 里。
    """
    override = os.environ.get(ENV_PATH)
    if override:
        return Path(override).expanduser()
    text = str(db_path or "")
    if text in ("", ":memory:"):
        return None
    return Path(text).expanduser().resolve().parent / FILENAME


def log(cmd: str, payload: dict) -> None:
    """审计入口。``payload`` 放 argv / now / parsed / before / after / exit / error 等。

    任何异常都在这里吃掉——审计写不进去是小事，命令结果不能因此改变。
    """
    try:
        path = path_for(payload.get("db"))
        if cmd not in AUDITED or path is None:
            return
        get_logger(path).info("", extra={"payload": _record(cmd, payload)})
    except Exception as exc:          # noqa: BLE001 —— 审计是旁路，绝不许拖垮主流程
        sys.stderr.write(f"oplog 写入失败：{exc}\n")


def get_logger(path) -> logging.Logger:
    """这个文件的 logger：同名 logger 只挂一个 handler，换路径就换 handler（幂等）。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target = str(Path(path))
    if _bound_to(logger) != target:
        _rebind(logger, target)
    return logger


def _record(cmd: str, payload: dict) -> dict:
    """一行审计记录；字段顺序固定，便于 grep / jq / 人直接读。"""
    code = int(payload.get("exit") or 0)
    record = {
        "at": wall_clock().isoformat(timespec="seconds"),
        "cmd": cmd,
        "argv": ["gtd", *[_clip(item) for item in payload.get("argv") or ()]],
        "now": _iso(payload.get("now")),
        "parsed": payload.get("parsed"),
        "before": payload.get("before"),
        "after": payload.get("after"),
        "result": "ok" if code == 0 else "fail",
        "exit": code,
        "pid": os.getpid(),
        "session": os.environ.get(ENV_SESSION) or "",
    }
    if payload.get("error"):
        record["error"] = _clip(payload["error"])
    if payload.get("traceback"):
        record["traceback"] = list(payload["traceback"])[-KEEP_TB_LINES:]
    return record


def _rebind(logger: logging.Logger, target: str) -> None:
    """把 logger 唯一的 handler 指到 target；目录不在就建。"""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(target, maxBytes=MAX_BYTES,
                                  backupCount=BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)


def _bound_to(logger: logging.Logger) -> str | None:
    """当前 handler 指向哪个文件（没 handler 给 None）。"""
    for handler in logger.handlers:
        return getattr(handler, "baseFilename", None)
    return None


def _iso(moment) -> str | None:
    """业务时刻（``--now``）→ ISO；没注入就是 null，**不拿真实时间冒充**。"""
    if not isinstance(moment, datetime):
        return None
    return moment.isoformat(timespec="seconds")


def _clip(value):
    """字符串截到 200 字（其它类型原样返回）。"""
    if isinstance(value, str) and len(value) > KEEP_TEXT:
        return value[:KEEP_TEXT] + "…"
    return value
