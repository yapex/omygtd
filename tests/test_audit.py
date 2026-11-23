"""操作日志（`gtd/audit.py` + cli 接线）的测试：全部 FakeStore + 临时库。

三条重点：

1. **`at` 与 `now` 必须分开**——`at` 是真实墙钟，`now` 是 `--now` 注入的业务时间。
   2026-11-23 的事故就是两者混在一起（`moved_at` 写了 `--now` 的 16:00，Reminder 却是
   16:02 建的），`test_at_*` 就是那次的回归锁。
2. **日志落在库旁边**——`--db` 指到 `tmp_path` 时日志也落临时目录，一条都不许写进
   项目 `data/`。
3. **审计是旁路**——写日志失败 / handler 抛异常，命令的退出码与输出都不许变。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from gtd import audit, cli, reminders
from gtd.reminders import FakeStore

NOW = "2026-11-23 09:00"
CST = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_LOG = PROJECT_ROOT / "data" / "oplog.jsonl"


# ---------------------------------------------------------------- 夹具与工具

@pytest.fixture()
def no_guard(monkeypatch):
    """放开 reminders 的列表守卫：FakeStore 不碰真实 Reminder，拦不拦都一样。"""
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def fake(no_guard):
    return FakeStore(lists=("GTD", "Waiting"))


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


@pytest.fixture()
def log_path(db):
    return Path(db).parent / audit.FILENAME


def run(capsys, argv, *, db, store, now=NOW):
    """跑一次 cli.main，返回 (退出码, JSON 载荷)。"""
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


def stub_render():
    """替掉真 Chrome：写个假 PNG 就返回（断言只看日志，不看像素）。"""
    def fake_render(entries, *, now, out_path):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(out_path)
    return fake_render


def fake_board(capsys, db, store, tmp_path, monkeypatch) -> dict:
    """出一张假图（替掉真 Chrome），返回载荷（含 numbers）。"""
    monkeypatch.setattr(cli.render, "render_today", stub_render())
    return run(capsys, ["board", "--out", str(tmp_path / "b.png")],
               db=db, store=store)[1]


def entries(log_path) -> list[dict]:
    """读日志：一行一个 JSON（当文件不存在时是空列表）。"""
    path = Path(log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------- 落点

def test_log_lands_next_to_the_db_not_project_data(capsys, db, fake, log_path):
    """--db 指到临时库时，日志落临时目录：正式 data/ 一个字节都不许动。"""
    before = REAL_LOG.stat().st_mtime_ns if REAL_LOG.exists() else None
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    assert log_path.exists()
    after = REAL_LOG.stat().st_mtime_ns if REAL_LOG.exists() else None
    assert before == after


def test_memory_db_writes_no_log():
    """内存库没有目录可落，直接不写（否则会掉进项目 data/）。"""
    assert audit.path_for(":memory:") is None


def test_env_override_wins(monkeypatch, tmp_path):
    """GTD_LOG 优先于一切（测试/排障时换出口用）。"""
    target = tmp_path / "custom" / "x.jsonl"
    monkeypatch.setenv(audit.ENV_PATH, str(target))
    assert audit.path_for(":memory:") == target


def test_log_directory_is_gitignored():
    """日志永不进 git：data/ 必须在 .gitignore 里。"""
    assert "data/" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 一条改了什么都记下来

def test_add_writes_one_line_with_parsed_before_after(capsys, db, fake, log_path):
    code, _ = run(capsys, ["add", "明天下午交材料"], db=db, store=fake)
    assert code == 0
    rows = entries(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["cmd"] == "add" and row["result"] == "ok" and row["exit"] == 0
    assert row["argv"][0] == "gtd" and row["argv"][-1] == "明天下午交材料"
    assert "add" in row["argv"]
    assert row["parsed"]["title"] == "交材料"
    assert row["parsed"]["due"] == "2026-11-24" and row["parsed"]["slot"] == "pm"
    assert row["before"] is None                      # 新增，没有「改之前」
    assert row["after"]["source"] == "reminder" and row["after"]["reminder_id"]
    assert row["pid"] and "session" in row


def test_three_write_commands_each_log_one_line(capsys, db, fake, tmp_path, monkeypatch,
                                                log_path):
    """add / priority / done 连跑：日志按顺序三条，before/after 能看出改了什么。"""
    run(capsys, ["add", "明天下午交材料"], db=db, store=fake)      # 进 Reminder（图上 1 号）
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)              # 进收件箱（图上 2 号）
    board = fake_board(capsys, db, fake, tmp_path, monkeypatch)
    number = str(board["numbers"][0])
    run(capsys, ["priority", number, "high"], db=db, store=fake)
    run(capsys, ["done", number], db=db, store=fake)

    rows = entries(log_path)
    assert [row["cmd"] for row in rows] == ["add", "add", "priority", "done"]
    priority_row = rows[2]
    assert priority_row["before"]["priority"] == "none"
    assert priority_row["after"]["priority"] == "high"
    done_row = rows[3]
    assert done_row["before"][0]["completed"] is False
    assert done_row["after"][0]["ok"] is True


def test_clarify_records_inbox_before_and_after(capsys, db, fake, log_path):
    """今天的坑的正脸：moved_at / reminder_id 的「改前 / 改后」都在日志里。"""
    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    code, data = run(capsys, ["clarify", "1", "明天下午"], db=db, store=fake)
    assert code == 0
    row = entries(log_path)[-1]
    assert row["before"]["inbox_id"] == data["inbox_id"]
    assert row["before"]["moved_at"] is None and row["before"]["reminder_id"] is None
    assert row["after"]["moved_at"] == "2026-11-23T09:00:00"
    assert row["after"]["reminder_id"] == data["reminder_id"]


# ---------------------------------------------------------------- at 与 now 必须分开

def test_at_is_wall_clock_while_now_is_injected(capsys, db, fake, log_path, monkeypatch):
    """今天的坑的锁：`at` 是真实墙钟，`now` 才是 --now 的业务时间，两者必须不同。"""
    monkeypatch.setattr(audit, "wall_clock",
                        lambda: datetime(2026, 11, 23, 17, 15, 3, tzinfo=CST))
    run(capsys, ["add", "明天下午交材料"], db=db, store=fake, now="2026-11-23 16:00")
    row = entries(log_path)[0]
    assert row["at"] == "2026-11-23T17:15:03+08:00"
    assert row["now"] == "2026-11-23T16:00:00"
    assert row["at"] != row["now"]


def test_without_now_the_field_is_null(capsys, db, fake, log_path):
    """没注入 --now：now 为 null，绝不用真实时间冒名顶替。"""
    cli.main(["--db", str(db), "add", "买鸡蛋"], store=fake)
    capsys.readouterr()
    row = entries(log_path)[0]
    assert row["now"] is None and row["at"]


# ---------------------------------------------------------------- 失败也要记

def test_failed_command_is_logged_with_error(capsys, db, fake, log_path):
    """done 99（图上没这个号）→ 日志 result=fail + error，退出码仍是 1。"""
    code, data = run(capsys, ["done", "99"], db=db, store=fake)
    assert code == 1 and data["ok"] is False
    row = entries(log_path)[0]
    assert row["result"] == "fail" and row["exit"] == 1
    assert "#99" in row["error"]


def test_exception_path_logs_traceback(capsys, db, fake, log_path):
    """冒出来的异常（RuntimeError）也要留痕：error 原文 + traceback 末几行。"""
    class BoomStore(FakeStore):
        def ensure_list(self, name: str) -> None:      # 临时库只走单张 ensure_list
            raise RuntimeError("Reminder 后端崩了")

    run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    code = cli.main(["--db", str(db), "--now", NOW, "clarify", "1", "明天下午"],
                    store=BoomStore(lists=("GTD", "Waiting")))
    capsys.readouterr()
    assert code == 1
    row = entries(log_path)[-1]
    assert row["result"] == "fail" and "Reminder 后端崩了" in row["error"]
    assert 0 < len(row["traceback"]) <= audit.KEEP_TB_LINES
    assert any("RuntimeError" in line for line in row["traceback"])


# ---------------------------------------------------------------- 审计绝不影响主流程

def test_log_failure_does_not_change_the_command(capsys, db, fake, monkeypatch, log_path):
    """日志写不出来（handler 炸）：命令照常 ok、退出码 0，只在 stderr 提一句。"""
    def boom(_path):
        raise OSError("disk full")
    monkeypatch.setattr(audit, "get_logger", boom)

    code, data = run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert entries(log_path) == []


def test_unwritable_log_path_does_not_change_the_command(capsys, db, fake, tmp_path,
                                                         monkeypatch):
    """日志目录建不出来（/dev/null 下面）：同样只提示、不影响命令。"""
    monkeypatch.setenv(audit.ENV_PATH, "/dev/null/nope/oplog.jsonl")
    code, data = run(capsys, ["add", "买鸡蛋"], db=db, store=fake)
    assert code == 0 and data["ok"] is True


# ---------------------------------------------------------------- 格式与规模

def test_read_only_commands_write_nothing(capsys, db, fake, log_path):
    """只读命令不进 op-log：日志只记修改类。"""
    run(capsys, ["inbox"], db=db, store=fake)
    run(capsys, ["list"], db=db, store=fake)
    assert entries(log_path) == []


def test_long_text_is_clipped_in_argv(capsys, db, fake, log_path):
    """长文本截到 200 字（将来 --note 长文案也不会把日志撑爆）。"""
    run(capsys, ["add", "看" * 300], db=db, store=fake)
    row = entries(log_path)[0]
    assert row["argv"][-1].endswith("…")
    assert len(row["argv"][-1]) == audit.KEEP_TEXT + 1


def test_logger_binds_one_rotating_handler_per_path(tmp_path):
    """get_logger 幂等：同一个文件只挂一个 handler，且是 5 MB 滚动的那个。"""
    target = tmp_path / "deep" / audit.FILENAME
    audit.get_logger(target)
    audit.get_logger(target)
    logger = logging.getLogger(audit.LOGGER_NAME)
    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == audit.MAX_BYTES
    assert handler.backupCount == audit.BACKUP_COUNT
    assert Path(handler.baseFilename) == target
    assert target.parent.is_dir()
