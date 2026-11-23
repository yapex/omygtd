"""doctor 健康检查的测试：全 FakeStore + 临时文件库，零 Reminder 读写。

为什么不用 ``:memory:``：``Inbox`` / ``Mirror`` 各自持连接，两条 ``:memory:``
连接是**两个互不相干的库**（项目既有约定，见 test_cli_new、test_inbox），
所以跨实例共享要用 ``tmp_path`` 下的临时文件。

隔离：
1. ``store`` 全程 ``FakeStore``，绝不构造 ``RemindctlAdapter``
   （``test_check_never_touches_real_reminder`` 用 monkeypatch 把它钉死）。
2. ``reminders._guard_list`` 在 pytest 里拦 GTD/Waiting，FakeStore 也照拦 ——
   本文件把它放开，真实 adapter 根本走不到。
3. 出图耗时文件用 ``GTD_BOARD_STAMP`` 指到 tmp_path，不碰真实 data/。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from gtd import cli, doctor, reminders
from gtd.inbox import Inbox, Mirror
from gtd.reminders import FakeStore

NOW = datetime(2026, 11, 23, 9, 0)
LISTS = ("GTD", "Waiting")


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def no_guard(monkeypatch):
    """放开列表守卫：FakeStore 不碰真实 Reminder，拦不拦等价。"""
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def fake(no_guard):
    return FakeStore(lists=LISTS)


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


@pytest.fixture()
def stamp(tmp_path, monkeypatch):
    """耗时记录文件指到临时路径，顺带保证 record/read 用的是同一个文件。"""
    path = tmp_path / "board_seconds.json"
    monkeypatch.setenv("GTD_BOARD_STAMP", str(path))
    return path


class UnavailableStore(FakeStore):
    """Reminder 不可用的存储：构造时注入 ``available=False``。"""

    def __init__(self, lists=LISTS):
        super().__init__(lists=lists, available=False)


# ---------------------------------------------------------------- 工具

def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def seed(db, titles, *, age_days=0) -> None:
    """往 inbox 塞未迁出的念头；created_at 统一钉在 age_days 天前。"""
    inbox = Inbox(db)
    for title in titles:
        inbox.add(title, now=days_ago(age_days))
    inbox.close()


def add_remote(fake, title, *, list_name="GTD", day=23) -> str:
    return fake.add(title, datetime(2026, 11, day, 15, 0), list_name)


def run(capsys, argv, *, db, store, stamp, now="2026-11-23 09:00"):
    code = cli.main(["--db", str(db), "--now", now, *argv], store=store)
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- 1. 空库

def test_empty_db_is_healthy(db, fake, stamp):
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.inbox_lag_days == 0
    assert health.inbox_count == 0
    assert health.problems == []
    assert health.ok is True


# ---------------------------------------------------------------- 2/3. 积压边界

def test_inbox_lag_over_limit_is_a_problem(db, fake, stamp):
    seed(db, ["十五天前的念头"], age_days=15)
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.inbox_lag_days == 15
    assert "收件箱积压 15 天，该清了" in health.problems


def test_inbox_lag_at_limit_is_not_a_problem(db, fake, stamp):
    seed(db, ["十四天前的念头"], age_days=14)
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.inbox_lag_days == 14
    assert not any("积压" in item for item in health.problems)
    assert health.ok is True


# ---------------------------------------------------------------- 4. Reminder 不可用

def test_reminder_unavailable_is_a_problem(db, stamp):
    health = doctor.check(inbox_db=db, store=UnavailableStore(lists=LISTS), now=NOW)
    assert health.reminder_ok is False
    assert "Reminder 不可用，任务写不进去" in health.problems


# ---------------------------------------------------------------- 5/6. 镜像漂移

def test_mirror_drift_one_is_a_problem(db, fake, stamp):
    add_remote(fake, "只有 Reminder 里有")          # 远端 1 条，镜像 0 条
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert (health.mirror_count, health.remote_count) == (0, 1)
    assert health.mirror_drift == 1
    assert "本地镜像与 Reminder 差 1 条，建议重建" in health.problems


def test_mirror_in_sync_is_not_a_problem(db, fake, stamp):
    add_remote(fake, "一条一致的")
    doctor.rebuild_mirror(inbox_db=db, store=fake, now=NOW)
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.mirror_drift == 0
    assert health.ok is True


# ---------------------------------------------------------------- 7/8. 条数边界

def test_inbox_count_over_limit_is_a_problem(db, fake, stamp):
    seed(db, [f"念头 {i}" for i in range(31)])
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.inbox_count == 31
    assert "收件箱 31 条，超过 30 条说明没在整理" in health.problems


def test_inbox_count_at_limit_is_not_a_problem(db, fake, stamp):
    seed(db, [f"念头 {i}" for i in range(30)])
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.inbox_count == 30
    assert not any("超过 30 条" in item for item in health.problems)
    assert health.ok is True


# ---------------------------------------------------------------- 9. 重建归零

def test_rebuild_mirror_zeroes_drift(db, fake, stamp):
    add_remote(fake, "GTD 里的")
    add_remote(fake, "Waiting 里的", list_name="Waiting")
    add_remote(fake, "别的列表里的", list_name="GTD_TEST")
    before = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert before.mirror_drift == 2

    written = doctor.rebuild_mirror(inbox_db=db, store=fake, now=NOW)

    after = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert written == 2                       # GTD_TEST 不算在镜像范围内
    assert after.mirror_drift == 0
    assert after.problems == [] or all("镜像" not in item for item in after.problems)
    assert len(Mirror(db).all()) == 2


def test_rebuild_mirror_refuses_when_reminder_down(db, stamp):
    with pytest.raises(RuntimeError, match="拒绝重建"):
        doctor.rebuild_mirror(inbox_db=db, store=UnavailableStore(lists=LISTS), now=NOW)


def test_check_is_read_only(db, fake, stamp):
    """check 绝不自动修：镜像还是空的、念头还在收件箱（修复要人喊 rebuild_mirror）。"""
    seed(db, ["一条念头"])
    add_remote(fake, "Reminder 里的一条")
    doctor.check(inbox_db=db, store=fake, now=NOW)
    assert Mirror(db).all() == []
    assert len(Inbox(db).pending()) == 1


# ---------------------------------------------------------------- 10. 多条同时成立

def test_all_problems_are_listed_together(db, fake, stamp):
    seed(db, ["积压的念头"], age_days=20)
    seed(db, [f"塞满的念头 {i}" for i in range(30)])     # 合计 31 条
    add_remote(fake, "远端多出来的一条")
    stamp.write_text(json.dumps({"seconds": 7.25}), encoding="utf-8")

    health = doctor.check(inbox_db=db, store=fake, now=NOW)

    assert len(health.problems) == 4, health.problems
    assert "收件箱积压 20 天，该清了" in health.problems
    assert "本地镜像与 Reminder 差 1 条，建议重建" in health.problems
    assert "出图耗时 7.2 秒，偏慢" in health.problems
    assert "收件箱 31 条，超过 30 条说明没在整理" in health.problems


def test_unavailable_suppresses_drift_noise(db, stamp):
    """Reminder 挂了就别喊人「重建镜像」——拿 0 条比出来的差值是假的。"""
    health = doctor.check(inbox_db=db, store=UnavailableStore(lists=LISTS), now=NOW)
    assert health.mirror_drift == 0
    assert not any("建议重建" in item for item in health.problems)


# ---------------------------------------------------------------- 出图耗时

def test_board_seconds_missing_is_none(db, fake, stamp):
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.board_seconds is None
    assert not any("偏慢" in item for item in health.problems)


def test_record_board_seconds_roundtrip(db, fake, stamp, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)   # 模拟真机运行
    path = doctor.record_board_seconds(1.2345, now=NOW)
    assert path == stamp
    assert doctor.board_seconds() == 1.234


def test_record_board_seconds_is_quiet_under_pytest(tmp_path, monkeypatch):
    """pytest 里不写「真实 data/」：把 PROJECT_ROOT 指到 tmp 验证一个字节都没落。"""
    monkeypatch.delenv("GTD_BOARD_STAMP", raising=False)
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    assert doctor.record_board_seconds(9.0) is None
    assert not (tmp_path / "data" / "board_seconds.json").exists()


def test_corrupt_stamp_file_is_none(db, fake, stamp):
    stamp.write_text("{ nope", encoding="utf-8")
    assert doctor.board_seconds() is None


# ---------------------------------------------------------------- 零真实写入

def test_check_never_touches_real_reminder(db, fake, stamp, monkeypatch):
    """FakeStore 场景下连 status 探针都不许调（那会起子进程并读真实权限）。"""
    def boom(*args, **kwargs):
        raise AssertionError("check() 不该碰真实 Reminder")

    monkeypatch.setattr(reminders.subprocess, "run", boom)
    health = doctor.check(inbox_db=db, store=fake, now=NOW)
    assert health.reminder_ok is True


def test_real_adapter_probe_is_status_based(monkeypatch):
    """真机路径：adapter 自己跑 `status` 探针，doctor 只问 adapter。"""
    adapter = reminders.RemindctlAdapter(lists=LISTS)

    def status(stdout):
        return lambda args: SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(adapter, "_run", status("Full Access"))
    assert adapter.is_available() is True
    assert doctor._reminder_ok(adapter) is True

    monkeypatch.setattr(adapter, "_run", status("Denied"))
    assert doctor._reminder_ok(adapter) is False

    monkeypatch.setattr(adapter, "_run", lambda args: None)   # 超时 / 不可执行
    assert doctor._reminder_ok(adapter) is False


# ---------------------------------------------------------------- CLI

def test_cli_doctor_healthy_json(capsys, db, fake, stamp):
    code, payload = run(capsys, ["doctor"], db=db, store=fake, stamp=stamp)
    assert code == 0
    assert payload == {"ok": True, "inbox_lag_days": 0, "inbox_count": 0,
                       "reminder_ok": True, "mirror_drift": 0, "problems": []}


def test_cli_doctor_unhealthy_exits_1(capsys, db, stamp):
    code, payload = run(capsys, ["doctor"], db=db,
                        store=UnavailableStore(lists=LISTS), stamp=stamp)
    assert code == 1
    assert payload["ok"] is False
    assert payload["reminder_ok"] is False
    assert payload["problems"]


def test_cli_doctor_human_output(capsys, db, fake, stamp):
    code = cli.main(["--db", str(db), "--now", "2026-11-23 09:00",
                     "--human", "doctor"], store=fake)
    assert code == 0
    assert "健康" in capsys.readouterr().out


def test_cli_doctor_human_lists_problems(capsys, db, stamp):
    seed(db, ["十五天前"], age_days=15)
    code = cli.main(["--db", str(db), "--now", "2026-11-23 09:00",
                     "--human", "doctor"], store=FakeStore(lists=LISTS))
    out = capsys.readouterr().out
    assert code == 1
    assert "有问题" in out and "积压 15 天" in out


def test_cli_doctor_human_reports_schema_error(capsys, tmp_path, fake, stamp):
    """v1 老库（daily_seq 缺 ref）→ 错误原文要露出来，不能吞成「有问题」。"""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(str(old))
    conn.execute("CREATE TABLE daily_seq (day TEXT, seq INTEGER, task_id INTEGER)")
    conn.commit()
    conn.close()
    code = cli.main(["--db", str(old), "--now", "2026-11-23 09:00",
                     "--human", "doctor"], store=fake)
    out = capsys.readouterr().out
    assert code == 1
    assert "表结构不兼容" in out
