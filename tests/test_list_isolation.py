"""测试隔离（老板 2026-11-23 定的）：临时库的 Reminder 写入一律落 ``GTD_TEST``。

原来靠人记「测试记得写 GTD_TEST」，可 worker 不知道要先 `remindctl list GTD_TEST --create`，
一遇到 `List not found` 就绕去写真实 ``GTD`` —— 当天数据乱的直接原因。现在按 **db 路径**
硬判（``cli.target_list``），一处决定、add / clarify / 出图都引用它。

四个硬要求各有一条：
1. 临时库 → ``GTD_TEST``
2. 正式库 ``data/gtd.db``（含没传 --db）→ ``GTD``（Waiting 照旧）
3. ``GTD_TEST`` 不存在时自动建（FakeStore 模拟 `remindctl list --create`）
4. ``clarify`` 与 ``add`` 走同一个判断
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gtd import cli, reminders
from gtd.inbox import DEFAULT_DB
from gtd.reminders import TEST_LIST, FakeStore

NOW = "2026-11-23 09:00"


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def no_guard(monkeypatch):
    """放开列表守卫：FakeStore 不碰真实 Reminder，拦不拦都一样（同 test_cli_new）。"""
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def fake(no_guard):
    """假装已经有 GTD / Waiting，但**还没有** GTD_TEST。"""
    return FakeStore(lists=("GTD", "Waiting"))


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


def run(capsys, argv, *, db, store, now=NOW):
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


def lists_of(fake) -> set:
    return {item["listName"] for item in fake.all()}


# ---------------------------------------------------------------- 1 / 2：按库路径决定

def test_tmp_db_writes_land_in_the_test_list(capsys, db, fake):
    """临时库 + 带时间 → 写 GTD_TEST，真实 GTD / Waiting 一条都不多。"""
    code, data = run(capsys, ["add", "交材料", "--due", "2026-11-24", "--at", "12:00"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["list"] == TEST_LIST and data["source"] == "reminder"
    assert lists_of(fake) == {TEST_LIST}
    assert fake.pending("GTD") == [] and fake.pending("Waiting") == []


def test_prod_db_still_uses_the_real_lists(tmp_path):
    """正式库路径（含没传 --db 的默认值）→ 仍是 GTD（列表不再从话里解析）。

    纯函数级：正式库在 pytest 里不能开（Inbox 守卫），所以只验「决定列表」这一层。
    """
    assert cli.is_prod_db(DEFAULT_DB) is True
    assert cli.target_list(str(DEFAULT_DB)) == "GTD"
    assert cli.target_list(str(tmp_path / "x.db")) == TEST_LIST
    assert cli.is_prod_db(str(tmp_path / "x.db")) is False


def test_context_reports_the_isolation_for_a_tmp_db(db, fake):
    """Context 一处决定（list_name / test_db），别处只引用。"""
    args = SimpleNamespace(db=str(db), now=NOW)
    ctx = cli._context(args, fake, None)
    assert ctx.test_db is True and ctx.list_name == TEST_LIST


# ---------------------------------------------------------------- 3：列表不存在时自动建

def test_missing_test_list_is_created_before_writing(capsys, db, fake):
    """GTD_TEST 不存在 → 先 ``ensure_list``（= remindctl list --create），再写。"""
    assert TEST_LIST not in fake.lists
    code, data = run(capsys, ["add", "交材料", "--due", "2026-11-24", "--slot", "pm"], db=db, store=fake)
    assert code == 0 and data["list"] == TEST_LIST
    assert fake.created == [TEST_LIST]        # 只建了测试列表，没顺带建 GTD / Waiting
    assert TEST_LIST in fake.lists
    assert [item["title"] for item in fake.pending(TEST_LIST)] == ["交材料"]


def test_ensure_lists_on_prod_path_still_covers_both_lists():
    """正式库路径照旧走 ``ensure_lists()``（GTD + Waiting）；隔离只动临时库。"""
    class RecordingStore(FakeStore):
        def __init__(self):
            super().__init__(lists=())
            self.full_calls = 0

        def ensure_lists(self) -> None:
            self.full_calls += 1

    store = RecordingStore()
    cli._ensure_lists(SimpleNamespace(test_db=False, store=store))
    assert store.full_calls == 1 and store.created == []

    cli._ensure_lists(SimpleNamespace(test_db=True, store=store))
    assert store.full_calls == 1              # 临时库不再走全量 ensure
    assert store.created == [TEST_LIST]       # 只建测试列表


# ---------------------------------------------------------------- 4：clarify 同一条路

def test_clarify_uses_the_same_rule(capsys, db, fake):
    """收件箱念头补时间 → 同样落 GTD_TEST（不是只有 add 走这条路）。"""
    run(capsys, ["add", "回设计稿"], db=db, store=fake)          # 没时间 → 收件箱
    assert fake.all() == []
    code, data = run(capsys, ["clarify", "1", "--due", "2026-11-24", "--slot", "pm"], db=db, store=fake)
    assert code == 0 and data["ok"] is True
    assert data["list"] == TEST_LIST and data["source"] == "reminder"
    assert lists_of(fake) == {TEST_LIST}
    assert [item["title"] for item in fake.pending(TEST_LIST)] == ["回设计稿"]
