"""写操作互斥锁：同一份本地库，同一时刻只许一个进程写。

背景（2026-11-23）：两张卡同时在同一个 Reminders 库里造测试数据，``remindctl list``
的条数在 16:00–16:07 之间来回跳（GTD 2→3→4→3）。老板的判据是「不信任文字纪律，
规则要落成可执行拦截」——所以并发防线落在这把锁上，不落在文档里。

**为什么用 ``fcntl.flock`` 而不是手写 pid 文件**：锁跟着打开的文件描述走，进程退出
（含被 SIGKILL）时内核自动释放，永远留不下「持锁进程早就不在了」的死锁；pid 文件得
靠人判断「这个号还活着吗、是不是被新进程复用了」，判断错就卡死。

粒度与边界：
- 一把全局锁 ``<db 所在目录>/.gtd.lock``，只锁**写**命令（``add`` / ``clarify`` /
  ``done`` / ``priority``）；``view`` / ``board`` / ``list`` / ``doctor`` 只读，不加锁。
- 锁文件与 db 同级，所以 ``--db /tmp/x.db`` 与正式库各锁各的，测试不互相阻塞。
- 锁文件里写持有者（pid / 时刻 / 命令行）**只为人排查**；判断谁持有锁只看 flock，
  绝不读这个文件做判据。
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .inbox import PROJECT_ROOT

LOCK_NAME = ".gtd.lock"
WAIT_SECONDS = 10.0                     # 拿不到锁最多等这么久
POLL_SECONDS = 0.2                      # 重试间隔
WRITE_COMMANDS = frozenset({"add", "clarify", "done", "priority"})
KEEP_TAG = 120                          # 锁文件里命令行最多留这么多字符


class LockTimeout(RuntimeError):
    """等满 WAIT_SECONDS 还没拿到锁：另一个进程正在写，别硬闯。"""


def lock_path(db) -> Path | None:
    """锁文件跟 db 同级：``--db /tmp/x.db`` → ``/tmp/.gtd.lock``。

    内存库（``:memory:`` / 空）没有目录可放，也没有跨进程并发可防——**不落锁文件**。
    （否则 ``Path(":memory:").resolve().parent`` 会把锁落到当前工作目录，把仓库弄脏。）
    """
    text = str(db or "")
    if text in ("", ":memory:"):
        return None
    return Path(text).expanduser().resolve().parent / LOCK_NAME


@contextmanager
def write_lock(db, *, timeout: float | None = None, poll: float = POLL_SECONDS, tag: str = ""):
    """拿到写锁再干活；正常结束、异常、被信号打断都要释放（``with`` 兜住）。"""
    path = lock_path(db)
    if path is None:                        # 内存库：每个进程各自一份，没有并发可防
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        wait = WAIT_SECONDS if timeout is None else timeout
        _acquire(fd, path, wait, poll)
        _stamp(fd, tag)
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire(fd: int, path: Path, timeout: float, poll: float) -> None:
    """先试非阻塞锁，失败就每 poll 秒重试一次，等满 timeout 抛 LockTimeout。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockTimeout(busy_message(timeout, path)) from None
            time.sleep(min(poll, remaining))


def busy_message(timeout: float, path: Path) -> str:
    """等满锁的文案：照老板口径，并指出实际的锁文件在哪（默认库显示 data/.gtd.lock）。"""
    return (f"另一个 gtd 进程正在写（等了 {timeout:g} 秒）。"
            f"如果卡住了，看 {_shown(path)} 的持有者。")


def _shown(path: Path) -> str:
    """项目内的锁按相对路径显示（老板口径），别处的库给绝对路径。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _stamp(fd: int, tag: str = "") -> None:
    """把持有者写进锁文件，只为排查；写不进去也不许让命令失败。"""
    line = f"pid={os.getpid()} since={datetime.now():%Y-%m-%d %H:%M:%S}"
    if tag:
        line += " cmd=" + tag.replace("\n", " ")[:KEEP_TAG]
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (line + "\n").encode("utf-8"))
    except OSError:
        pass
