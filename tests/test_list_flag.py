"""``--list GTD|Waiting``（2026-11-24）：等对方回复这类要有入口。

四条硬要求：
1. 正式库路径下 ``--list Waiting`` 真的把 Waiting 传给 store；不给就是 GTD；
2. ``clarify`` 同一条路（念头澄清时也能进 Waiting）；
3. 非法值 / add 没 --due 就点名列表 / defer 传 --list → 退出码 2，一条都不写；
4. 隔离优先：临时库即使传了 ``--list Waiting`` 也只写 ``GTD_TEST``。
"""

from __future__ import annotations

import json

import pytest

from gtd import cli, reminders
from gtd.reminders import TEST_LIST, FakeStore

NOW = "2026-11-23 09:00"
DUE = "2026-12-01"


class RecordingStore(FakeStore):
    """记下 add 的实参，断言传给 store 的 listName。"""

    def __init__(self, lists=()):
        super().__init__(lists=lists)
        self.adds: list[dict] = []

    def add(self, title, due_at, list_name, **kwargs):
        self.adds.append({"title": title, "list": list_name, **kwargs})
        return super().add(title, due_at, list_name, **kwargs)


@pytest.fixture()
def no_guard(monkeypatch):
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def prod(monkeypatch, tmp_path):
    """把「正式库」挪到临时路径：走正式库分支，又不碰老板真库、不碰真实 Reminder。"""
    fake_prod = tmp_path / "prod" / "gtd.db"
    fake_prod.parent.mkdir()
    monkeypatch.setattr(cli, "DEFAULT_DB", str(fake_prod))
    return fake_prod


@pytest.fixture()
def store(no_guard):
    return RecordingStore()


def run(capsys, argv, *, db, store):
    code = cli.main(["--db", str(db), "--now", NOW, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- 1：add 进 Waiting

def test_add_waiting_passes_waiting_to_the_store(capsys, prod, store):
    code, data = run(capsys, ["add", "等对方报价", "--due", DUE, "--list", "Waiting"],
                     db=prod, store=store)
    assert code == 0 and data["ok"] is True
    assert store.adds[-1]["list"] == "Waiting"
    assert data["list"] == "Waiting"
    assert [item["title"] for item in store.pending("Waiting")] == ["等对方报价"]


def test_add_defaults_to_gtd(capsys, prod, store):
    code, data = run(capsys, ["add", "交材料", "--due", DUE], db=prod, store=store)
    assert code == 0 and store.adds[-1]["list"] == "GTD" and data["list"] == "GTD"


def test_waiting_list_is_created_when_missing(capsys, prod, store):
    """create-if-missing 跟着写入口径走：写 Waiting 就先建 Waiting（不是只建 GTD）。"""
    assert "Waiting" not in store.lists
    run(capsys, ["add", "等审批", "--due", DUE, "--list", "Waiting"], db=prod, store=store)
    assert store.created == ["Waiting"] and "Waiting" in store.lists
    assert store.pending("Waiting")


# ---------------------------------------------------------------- 2：clarify 进 Waiting

def test_clarify_waiting(capsys, prod, store):
    run(capsys, ["add", "等对方回话"], db=prod, store=store)          # 没时间 → 收件箱
    assert store.all() == []
    code, data = run(capsys, ["clarify", "1", "--due", DUE, "--list", "Waiting"],
                     db=prod, store=store)
    assert code == 0 and data["list"] == "Waiting"
    assert store.adds[-1]["list"] == "Waiting"
    assert [item["title"] for item in store.pending("Waiting")] == ["等对方回话"]


def test_clarify_defaults_to_gtd(capsys, prod, store):
    run(capsys, ["add", "回设计稿"], db=prod, store=store)
    code, data = run(capsys, ["clarify", "1", "--due", DUE], db=prod, store=store)
    assert code == 0 and data["list"] == "GTD"


# ---------------------------------------------------------------- 3：参数错 → 2

@pytest.mark.parametrize("value", ["waiting", "Someday", "等待", ""])
def test_bad_list_value_is_param_error(capsys, prod, store, value):
    code, data = run(capsys, ["add", "等报价", "--due", DUE, "--list", value],
                     db=prod, store=store)
    assert code == 2 and data["ok"] is False
    assert "Waiting" in data["error"] and "例" in data["error"]
    assert store.adds == [] and store.all() == []


def test_add_list_without_due_is_param_error(capsys, prod, store):
    code, data = run(capsys, ["add", "等报价", "--list", "Waiting"], db=prod, store=store)
    assert code == 2 and "--due" in data["error"]
    assert store.adds == []


def test_defer_has_no_list_flag(capsys, prod, store):
    """defer 只改期、不改归属：传 --list 是未识别参数（argparse → 2）。"""
    code = cli.main(["--db", str(prod), "--now", NOW,
                     "defer", "1", "--due", DUE, "--list", "Waiting"], store=store)
    assert code == 2 and store.rescheduled == []


# ---------------------------------------------------------------- 4：隔离优先

def test_tmp_db_ignores_list_and_writes_the_test_list(capsys, tmp_path, store):
    """临时库仍只写 GTD_TEST：--list Waiting 不能把测试任务塞进真实 Waiting。"""
    db = tmp_path / "gtd.db"
    code, data = run(capsys, ["add", "等报价", "--due", DUE, "--list", "Waiting"],
                     db=db, store=store)
    assert code == 0 and data["list"] == TEST_LIST and store.adds[-1]["list"] == TEST_LIST
    assert store.pending("Waiting") == []
