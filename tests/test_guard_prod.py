"""正式库守卫（``cli._guard_prod_db``）：worker 不许默认连 ``data/gtd.db``。

2026-11-23 事故回归：卡 t_84767dcd 的 worker 跑 ``gtd clarify 7``（当时是口语
``clarify 7 "明天下午"``，口语入口现已废除）却没带 ``--db``，
**没带 ``--db``**，直接打了正式库，把老板一条收件箱念头澄清进了 Reminder。
本文件锁五件事：① 拦 worker（含只有 ``HERMES_KANBAN_WORKSPACE`` 的真实子 shell）
② ``--db`` / ``GTD_DB`` 指到别处就放行 ③ 人手动跑放行 ④ ``GTD_ALLOW_PROD=1`` 是逃生门
⑤ 只拦写命令，``view`` / ``board`` / ``list`` / ``doctor`` 放行。

全部 FakeStore + ``tmp_path`` 库；``GTD_LOG`` 钉到 tmp，被拒时的 oplog 也不灌进真 ``data/``。
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from gtd import cli, reminders
from gtd.inbox import DEFAULT_DB, Inbox
from gtd.reminders import FakeStore

# 与 cli.WORKER_ENV 同一组名字，但这里写死：改了判据就该改测试，别让测试跟着代码跑
WORKER_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
              "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB")
CLEARED = (*WORKER_ENV, "GTD_DB", "GTD_ALLOW_PROD")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """干净环境：人身上的 worker 标记与 GTD_* 全清掉，oplog 钉到 tmp。"""
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GTD_LOG", str(tmp_path / "oplog.jsonl"))
    return monkeypatch


@pytest.fixture()
def no_guard(monkeypatch):
    """FakeStore 不碰真实 Reminder，列表守卫放开；真 adapter 走不到。"""
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def fake(no_guard):
    return FakeStore(lists=("GTD", "Waiting"))


def args(command="add", db=None) -> Namespace:
    """``_guard_prod_db`` 只读 ``command`` 与 ``db``，不需要完整 parser 结果。"""
    return Namespace(command=command, db=db)


# ---------------------------------------------------------------- 拒

def test_worker_without_db_is_refused(env):
    """worker + 不传 --db → 拒，而且错误信息直接告诉人怎么办。"""
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    with pytest.raises(RuntimeError) as exc:
        cli._guard_prod_db(args())
    message = str(exc.value)
    assert "禁止连正式库" in message
    assert "--db" in message and "GTD_ALLOW_PROD=1" in message


def test_worker_marker_without_task_var_is_refused(env):
    """实测：worker 的 terminal 子 shell 里**没有** HERMES_KANBAN_TASK，只有 WORKSPACE。"""
    env.setenv("HERMES_KANBAN_WORKSPACE", "/Users/yapex/.hermes/kanban/workspaces/t_x")
    with pytest.raises(RuntimeError, match="禁止连正式库"):
        cli._guard_prod_db(args())


def test_db_spelled_differently_still_counts_as_prod(env):
    """``--db`` 指到正式库的另一种写法（绕路径）绕不过去：比的是 resolve() 后的路径。"""
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    spelled = str(Path(DEFAULT_DB.parent) / "." / DEFAULT_DB.name)
    with pytest.raises(RuntimeError, match="禁止连正式库"):
        cli._guard_prod_db(args(db=spelled))


def test_refusal_takes_the_json_exit_1_path(env, fake, capsys):
    """走 main 的完整路径：退出码 1 + ``{"ok": false, "error": ...}``，正式库一个字节不动。"""
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    before = _fingerprint(DEFAULT_DB)
    code = cli.main(["add", "测试"], store=fake)
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False and "禁止连正式库" in payload["error"]
    assert _fingerprint(DEFAULT_DB) == before, "被拒的命令碰了正式库文件"
    assert _ops() == 1, "被拒的尝试应当留一条 oplog（落在 GTD_LOG 指的 tmp 里）"


# ---------------------------------------------------------------- 放行

def test_worker_with_db_flag_is_allowed(env, fake, tmp_path, capsys):
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    db = tmp_path / "x.db"
    code = cli.main(["--db", str(db), "--now", "2026-11-23 09:00", "add", "买鸡蛋"], store=fake)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    with Inbox(db) as box:
        assert [row["title"] for row in box.pending()] == ["买鸡蛋"]


def test_worker_with_gtd_db_env_is_allowed(env, fake, tmp_path, capsys):
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    env.setenv("GTD_DB", str(tmp_path / "env.db"))
    code = cli.main(["--now", "2026-11-23 09:00", "add", "买鸡蛋"], store=fake)
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"] is True


def test_human_run_without_db_is_allowed(env):
    """老板自己敲命令：没有 worker 标记 → 守卫直接放过（不能拦真人）。"""
    assert cli._in_kanban_worker() is False
    cli._guard_prod_db(args())            # 不抛就是放行


def test_allow_prod_env_is_the_escape_hatch(env):
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    env.setenv("GTD_ALLOW_PROD", "1")
    cli._guard_prod_db(args())            # 显式豁免：确需动真库的卡（如 doctor 体检）


@pytest.mark.parametrize("command", ["view", "board", "list", "doctor"])
def test_read_commands_pass_even_in_worker(env, command):
    """只拦写命令：只读命令对正式库照旧。"""
    env.setenv("HERMES_KANBAN_TASK", "t_x")
    cli._guard_prod_db(args(command=command))


# ---------------------------------------------------------------- 小工具

def _fingerprint(path: Path):
    """正式库的大小 + mtime；同进程内比对够用（被写就会变）。"""
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _ops() -> int:
    """GTD_LOG 里落了几条 oplog（被拒的尝试也要留痕）。"""
    log = Path(os.environ["GTD_LOG"])
    if not log.exists():
        return 0
    return len([line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()])
