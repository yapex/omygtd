"""写锁（``gtd/lock.py``）：并发两写串行、等锁、超时报错、只读不锁、异常也释放。

全部用 FakeStore + ``tmp_path`` 文件库，零 Reminder 写入；锁文件落在 ``tmp_path``，
不会碰正式库的 ``data/.gtd.lock``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gtd import cli, lock, reminders
from gtd.inbox import Inbox
from gtd.reminders import FakeStore

NOW = "2026-11-23 09:00"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def no_guard(monkeypatch):
    """FakeStore 不碰真实 Reminder，列表守卫放开；真 adapter 走不到。"""
    monkeypatch.setattr(reminders, "FORBIDDEN_IN_TESTS", set())


@pytest.fixture()
def fake(no_guard):
    return FakeStore(lists=("GTD", "Waiting"))


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "gtd.db"


def run(argv, *, db, store, now=NOW):
    return cli.main(["--db", str(db), "--now", now, *argv], store=store)


def titles(db):
    with Inbox(db) as box:
        return [row["title"] for row in box.pending()]


def parse_all(text):
    """stdout 里可能连着好几段 JSON（``indent=2`` 多行），逐个解开。"""
    decoder, items, index = json.JSONDecoder(), [], 0
    while (start := text.find("{", index)) >= 0:
        item, index = decoder.raw_decode(text, start)
        items.append(item)
    return items


def holder_thread(db, seconds):
    """后台线程持锁若干秒；返回「已拿到锁」的 event（避免测试和线程抢跑）。"""
    holding = threading.Event()

    def hold():
        with lock.write_lock(db, timeout=2.0):
            holding.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    return holding, thread


# ---------------------------------------------------------------- 粒度与位置

def test_write_commands_are_exactly_the_four_write_paths():
    """写锁只认四个写命令；view / board / list / doctor 一个都不许进来。"""
    assert set(lock.WRITE_COMMANDS) == {"add", "clarify", "done", "priority"}


def test_lock_file_lives_next_to_the_db(tmp_path):
    """锁跟 db 同级：不同库各自一把锁，测试与正式库互不阻塞。"""
    assert lock.lock_path(tmp_path / "gtd.db") == tmp_path.resolve() / ".gtd.lock"
    nested = tmp_path / "sub" / "x.db"
    assert lock.lock_path(nested) == (tmp_path / "sub").resolve() / ".gtd.lock"


def test_memory_db_leaves_no_lock_file_behind(tmp_path, fake, monkeypatch):
    """``--db :memory:`` 没有目录、也没有跨进程并发可防：不许往当前目录丢锁文件。"""
    monkeypatch.chdir(tmp_path)
    assert lock.lock_path(":memory:") is None
    with lock.write_lock(":memory:"):
        pass
    assert run(["add", "内存库里写一条"], db=":memory:", store=fake) == 0
    assert not (tmp_path / ".gtd.lock").exists()


# ---------------------------------------------------------------- 并发两写

def test_two_concurrent_writes_both_succeed(db, fake, capsys):
    """两个进程同时写：串行执行，两条都落地，谁也没覆盖谁。"""
    codes: list[int] = []
    start = threading.Barrier(2)

    def one(text):
        start.wait(5)                       # 尽可能同时出发
        codes.append(run(["add", text], db=db, store=fake))

    threads = [threading.Thread(target=one, args=(title,))
               for title in ("并发测试A", "并发测试B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    payloads = parse_all(capsys.readouterr().out)
    assert codes == [0, 0]
    assert len(payloads) == 2 and all(item["ok"] for item in payloads)
    assert sorted(item["seq"] for item in payloads) == [1, 2]      # 序号没撞车
    assert set(titles(db)) == {"并发测试A", "并发测试B"}            # 两条都在


# ---------------------------------------------------------------- 等锁 / 超时

def test_write_waits_for_a_held_lock(db, fake):
    """锁被占时等待（不是抢进去），持有者放锁后照样写成功。"""
    holding, thread = holder_thread(db, 0.5)
    assert holding.wait(5)
    began = time.monotonic()
    code = run(["add", "等锁之后写"], db=db, store=fake)
    waited = time.monotonic() - began
    thread.join(5)

    assert code == 0
    assert waited >= 0.3, f"没等锁就写进去了：{waited:.3f}s"
    assert "等锁之后写" in titles(db)


def test_write_times_out_after_waiting_and_exits_1(db, fake, monkeypatch, capsys):
    """等满时限还拿不到锁：退出码 1 + 那句固定文案（不许硬闯）。"""
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0.3)
    holding, thread = holder_thread(db, 1.5)
    assert holding.wait(5)

    began = time.monotonic()
    code = run(["add", "等不到锁"], db=db, store=fake)
    took = time.monotonic() - began
    thread.join(5)

    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["ok"] is False
    assert data["error"].startswith("另一个 gtd 进程正在写（等了 0.3 秒）")
    assert str(lock.lock_path(db)) in data["error"] and data["error"].endswith("的持有者。")
    assert 0.2 <= took < 1.3, f"等待时长不对：{took:.3f}s"
    assert "等不到锁" not in titles(db)          # 超时就不许写


# ---------------------------------------------------------------- 只读不锁

def test_read_commands_are_not_blocked_by_the_lock(db, fake):
    """持锁期间 view / inbox / list / doctor 都不许被阻塞。"""
    holding, thread = holder_thread(db, 1.0)
    assert holding.wait(5)
    for argv in (["inbox"], ["view"], ["list"], ["doctor"]):
        began = time.monotonic()
        code = run(argv, db=db, store=fake)
        took = time.monotonic() - began
        assert took < 0.5, f"{argv} 被写锁挡住了：{took:.3f}s"
        assert code in (0, 1)
    thread.join(5)


# ---------------------------------------------------------------- 释放与自愈

class ExplodingStore(FakeStore):
    """Reminder 写到一半炸掉：验异常路径也把锁放了。"""

    def add(self, *args, **kwargs):
        raise RuntimeError("Reminder 炸了")


def test_lock_is_released_when_the_command_raises(db, no_guard):
    code = run(["add", "明天 9 点开会"],
               db=db, store=ExplodingStore(lists=("GTD", "Waiting")))
    assert code == 1
    with lock.write_lock(db, timeout=0.5):        # 拿不到就是没释放
        pass


def test_lock_file_records_the_holder_for_troubleshooting(db):
    """报错文案让人「看锁的持有者」，所以锁文件里得真有 pid 与命令行。"""
    with lock.write_lock(db, tag="add 并发测试A"):
        path = lock.lock_path(db)
        assert path is not None
        text = path.read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in text and "cmd=add 并发测试A" in text


def test_killed_holder_leaves_no_permanent_lock(db):
    """flock 的锁随进程退出由内核释放：SIGKILL 也留不下永久死锁。"""
    helper = db.parent / "hold_lock.py"
    helper.write_text(
        "import sys, time\n"
        "from gtd import lock\n"
        "with lock.write_lock(sys.argv[1]):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(60)\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen([sys.executable, str(helper), str(db)],
                            stdout=subprocess.PIPE, env=env, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        proc.kill()                                # SIGKILL：进程来不及做任何清理
        proc.wait(5)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()

    began = time.monotonic()
    with lock.write_lock(db, timeout=2.0):
        pass
    assert time.monotonic() - began < 0.5
