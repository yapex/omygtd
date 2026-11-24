"""集成测试：真起子进程跑 ``gtd``，证明 CLI 能被外部调用（不只是 ``cli.main()`` 可测）。

约定：
- **全部用 ``tmp_path`` 临时库**（``--db``），不碰 ``data/gtd.db``。
- **全部不碰真实 Reminder**。带时间的 ``add`` 会写 Reminder，而 Reminder 是全局的、
  没有临时库，所以在 pytest 里由 ``reminders._guard_list`` 拦下（见
  ``test_add_with_time_is_refused_under_pytest``）。因此跨进程的成功路径只走
  「无时间 → 收件箱」；``board`` 需要读 Reminder，用子进程里注入 ``FakeStore`` 的
  小驱动脚本来出图（仍是真子进程 + 真 Chrome）。
- 只依赖 ``gtd.inbox.PROJECT_ROOT``（v2 存储层），不 import v1 的 ``gtd.store``。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gtd.inbox import PROJECT_ROOT
from gtd.render import _find_chrome
from gtd.reminders import TEST_LIST, RemindctlAdapter, _resolve_exe

REMINDCTL = _resolve_exe("remindctl") or "remindctl"

VERSION_RE = re.compile(r"gtd \d+\.\d+\.\d+")
PNG_MIN_BYTES = 10 * 1024

BOARD_DRIVER = textwrap.dedent(
    '''
    """子进程驱动：注入内存 FakeStore，跑真 CLI + 真 Chrome 出图（零 Reminder 写入）。"""
    import sys
    from datetime import datetime

    from gtd import cli, reminders
    from gtd.reminders import TEST_LIST, FakeStore

    reminders.FORBIDDEN_IN_TESTS = set()
    fake = FakeStore(lists=(TEST_LIST,))
    fake.add("交材料", datetime(2026, 11, 23, 12, 0), TEST_LIST, advance_minutes=0)
    fake.add("回邮件", datetime(2026, 11, 22, 18, 0), TEST_LIST)
    fake.add("买鸡蛋", datetime(2026, 11, 24, 9, 0), TEST_LIST)
    sys.exit(cli.main(sys.argv[1:], store=fake))
    '''
)


# ---------------------------------------------------------------- 工具

def _child_env(tmp_path: Path, **extra: str) -> dict:
    """子进程环境：钉住 pytest 标记（Reminder 守卫靠它）、隔离 out / 库路径。

    ``GTD_DB`` 一律清掉：本文件只用 ``--db`` 显式指定临时库，不让环境变量漏进正式库。
    """
    env = {key: value for key, value in os.environ.items() if key != "GTD_DB"}
    env.update({
        "PYTEST_CURRENT_TEST": "tests/test_integration.py::cross-process",
        "GTD_OUT": str(tmp_path / "out"),
        "PYTHONPATH": str(PROJECT_ROOT),
    })
    env.update(extra)
    return env


def _gtd(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    """跨进程跑 ``python -m gtd``（与 bin/gtd 同入口），返回完整结果。"""
    proc = subprocess.run([sys.executable, "-m", "gtd", *argv],
                          capture_output=True, text=True, timeout=120,
                          cwd=str(PROJECT_ROOT), env=_child_env(tmp_path))
    return proc


def _payload(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)


# ---------------------------------------------------------------- 1 / 2 / 3

def test_add_without_time_lands_in_inbox_cross_process(tmp_path):
    """跨进程 ``add``（无时间）→ JSON ``ok:true``，``seq`` 是整数，落临时库。"""
    db = tmp_path / "gtd.db"
    proc = _gtd(tmp_path, "--db", str(db), "--now", "2026-11-23 09:00",
                "add", "买鸡蛋")
    assert proc.returncode == 0, proc.stderr
    data = _payload(proc)
    assert data["ok"] is True
    assert isinstance(data["seq"], int) and data["seq"] == 1
    assert data["source"] == "inbox" and data["due"] is None
    assert db.exists()


def test_inbox_after_add_sees_the_thought_cross_process(tmp_path):
    """同一个临时库跑两次进程：后一次 ``inbox`` 能看到前一次写进去的念头。

    v2 的 inbox 载荷是 ``ok / count / oldest_days / items``（没有 v1 的 ``archived``
    键，归档行只是不再出现在 ``items`` 里），这里按 v2 契约断言。
    """
    db = tmp_path / "gtd.db"
    assert _gtd(tmp_path, "--db", str(db), "add", "买鸡蛋").returncode == 0
    inbox = _gtd(tmp_path, "--db", str(db), "inbox")
    assert inbox.returncode == 0, inbox.stderr
    data = _payload(inbox)
    assert data["ok"] is True
    assert set(data) == {"ok", "count", "oldest_days", "items"}
    assert data["count"] == 1
    assert [item["title"] for item in data["items"]] == ["买鸡蛋"]
    assert data["items"][0]["seq"] == 1


def test_add_with_time_lands_in_the_test_list(tmp_path):
    """带时间的 ``add`` 在 pytest 里走**测试列表**：写进 GTD_TEST，真实 GTD 一条不多。

    2026-11-23 之前这个用例断言「被守卫拦下」，而那正是坑：worker 被拦之后绕去直接写
    ``GTD``。现在临时库由 ``cli.target_list`` 自动改投 ``GTD_TEST``（列表不存在就
    自动 ``list --create``），所以跨进程的成功路径既安全、又能真测。
    """
    db = tmp_path / "gtd.db"
    title = "ZZ测试勿动跨进程临时库"
    proc = _gtd(tmp_path, "--db", str(db), "add", title,
                "--due", "2026-11-24", "--at", "12:00")
    assert proc.returncode == 0, proc.stderr
    data = _payload(proc)
    assert data["ok"] is True and data["list"] == TEST_LIST and data["title"] == title
    rid = data["reminder_id"]
    try:
        assert title in _open_titles(TEST_LIST)
        assert title not in _open_titles("GTD")
    finally:                                  # 测试条目自己收尾：删掉，别留在 GTD_TEST
        RemindctlAdapter().delete(rid)
    assert title not in _open_titles(TEST_LIST)


def _open_titles(list_name: str) -> list[str]:
    """直接问 remindctl（读操作）某张列表里有哪些未完成条目的标题。"""
    proc = subprocess.run([REMINDCTL, "show", "open", "--list", list_name, "--json"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return []
    return [str(item.get("title")) for item in json.loads(proc.stdout or "[]")
            if isinstance(item, dict)]


# ---------------------------------------------------------------- 4

@pytest.mark.skipif(_find_chrome() is None, reason="本机没有 Chrome，无法出 PNG")
def test_board_renders_png_cross_process(tmp_path):
    """``gtd board --out`` 跨进程出真 PNG（>10KB）：注入 FakeStore，零 Reminder 写入。"""
    driver = tmp_path / "board_driver.py"
    driver.write_text(BOARD_DRIVER, encoding="utf-8")
    out = tmp_path / "x.png"
    proc = subprocess.run(
        [sys.executable, str(driver), "--db", str(tmp_path / "gtd.db"),
         "--now", "2026-11-23 09:00", "board", "--out", str(out)],
        capture_output=True, text=True, timeout=180,
        cwd=str(PROJECT_ROOT), env=_child_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["path"] == str(out)
    assert data["count"] == 3 and data["overdue"] == 1   # 今天 + 逾期 + 明天（接下来区）
    assert out.exists() and out.stat().st_size > PNG_MIN_BYTES


# ---------------------------------------------------------------- 5

def test_version_cross_process(tmp_path):
    """``gtd --version`` 退出 0，输出含版本号（``gtd X.Y.Z``）。"""
    proc = _gtd(tmp_path, "--version")
    assert proc.returncode == 0, proc.stderr
    assert VERSION_RE.search(proc.stdout), proc.stdout
