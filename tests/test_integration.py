"""集成测试：用专用测试库文件 `data/test/gtd.db`（可随时清空）。

只有需要**真实文件行为**或**跨进程**的用例才放这里；其余单测一律 `:memory:`。
每个用例开始前清表；正式库另有 `Store` 的 pytest 守卫兜底。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from gtd.store import PROJECT_ROOT, Store, integration_db_path, prod_db_path

DATA_TABLES = ("tasks", "tasks_archive", "daily_seq", "undo_log")


def _clear(store) -> None:
    for table in DATA_TABLES:
        store.query(f"delete from {table}")
    try:
        store.query("delete from sqlite_sequence")
    except sqlite3.OperationalError:
        pass


@pytest.fixture()
def test_store(tmp_path, monkeypatch):
    path = integration_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GTD_TEST_DB", str(path))
    monkeypatch.setenv("GTD_DB", str(path))
    monkeypatch.setenv("GTD_CAPTURE", str(path.parent / "capture.jsonl"))
    monkeypatch.setenv("GTD_OUT", str(tmp_path / "out"))
    store = Store(str(path), path.parent / "capture.jsonl")
    store.init()
    _clear(store)
    yield store
    store.close()


def _gtd(*argv):
    proc = subprocess.run([sys.executable, "-m", "gtd", *argv],
                          capture_output=True, text=True, timeout=120,
                          cwd=str(PROJECT_ROOT))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_test_db_is_real_file_in_wal(test_store):
    """测试库是真文件、走 WAL，身份报 test（不是 prod）。"""
    assert test_store.db_path == integration_db_path()
    assert test_store.db_path != prod_db_path()
    assert test_store.db_path.exists()
    assert test_store.db_kind() == "test"
    assert test_store.journal_mode() == "wal"


def test_cleared_between_cases(test_store):
    """夹具每个用例前清表：进来就该是空的。"""
    assert test_store.count_tasks() == 0
    test_store.add("只在本题存在", due="2026-11-23")
    assert test_store.count_tasks() == 1


def test_cross_process_add_lands_in_test_db(test_store):
    """跨进程写：环境变量 GTD_DB 指向测试库，数据落在测试库而不是正式库。"""
    _gtd("add", "跨进程一条", "--due", "2026-11-23")
    assert test_store.count_tasks() == 1
    assert test_store.get(1)["title"] == "跨进程一条"


def test_db_flag_works_cross_process(test_store):
    """`gtd --db <path> doctor`：显式 --db 生效，并报出 db_kind=test。"""
    data = json.loads(_gtd("--db", str(integration_db_path()), "doctor"))
    assert data["db_kind"] == "test"
    assert data["db"] == str(integration_db_path())


def test_board_md_runs_on_test_db(test_store):
    """`gtd --db <test db> board --view today --format md` 能跑通。"""
    test_store.add("今天到期的一条", due="2026-11-22")
    out = _gtd("--db", str(integration_db_path()), "board", "--view", "today",
               "--format", "md", "--day", "2026-11-22")
    assert "今天到期的一条" in out
