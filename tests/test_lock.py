import fcntl
import os
from datetime import UTC, datetime, timedelta

import pytest

from edupagetasks.lock import RunLock, RunLockError


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "run.lock")


def test_acquire_and_release(path):
    lock = RunLock(path)
    assert lock.acquire() is True
    assert lock.acquire() is True  # idempotent while already held
    lock.release()
    assert lock.acquire() is True
    lock.release()


def test_second_acquire_denied_while_held(path):
    first = RunLock(path)
    second = RunLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_owner_recorded(path):
    lock = RunLock(path)
    assert lock.acquire() is True
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert f"pid={os.getpid()}" in content
    assert "started=" in content
    assert (os.stat(path).st_mode & 0o777) == 0o600
    lock.release()


def test_recorded_dead_pid_cannot_override_live_flock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, b"pid=99999999\nstarted=2020-01-01T00:00:00+00:00\n")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        lock = RunLock(path)
        assert lock.acquire() is False
        lock.release()
    finally:
        os.close(fd)


def test_live_holder_not_broken_by_age(path):
    old = datetime.now(UTC) - timedelta(hours=10)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, f"pid={os.getpid()}\nstarted={old.isoformat()}\n".encode())
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        lock = RunLock(path, stale_timeout_s=60)
        assert lock.acquire() is False
    finally:
        os.close(fd)


def test_old_ownerless_file_cannot_override_live_flock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, b"garbage, no readable owner pid\n")
    fcntl.flock(fd, fcntl.LOCK_EX)
    old = datetime.now(UTC) - timedelta(hours=10)
    os.utime(path, (old.timestamp(), old.timestamp()))
    try:
        lock = RunLock(path, stale_timeout_s=60)
        assert lock.acquire() is False
        lock.release()
    finally:
        os.close(fd)


def test_two_contender_acquire_single_winner(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, b"pid=99999999\nstarted=2020-01-01T00:00:00+00:00\n")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        first, second = RunLock(path), RunLock(path)
        assert first.acquire() is False
        assert second.acquire() is False
        fcntl.flock(fd, fcntl.LOCK_UN)
        results = [first.acquire(), second.acquire()]
        assert sum(1 for r in results if r) == 1
        assert [f for f in (first._fd, second._fd) if f is not None].__len__() == 1
        assert first.acquire() is True  # idempotent for the winner
        first.release()
    finally:
        os.close(fd)


def test_write_owner_failure_cleans_up_and_raises(path, monkeypatch):
    lock = RunLock(path)

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(RunLockError):
        lock.acquire()
    assert lock._fd is None
    assert not os.path.exists(path)


def test_fresh_lock_not_broken(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(
        fd, f"pid={os.getpid()}\nstarted={datetime.now(UTC).isoformat()}\n".encode()
    )
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        lock = RunLock(path, stale_timeout_s=3600)
        assert lock.acquire() is False
    finally:
        os.close(fd)


def test_context_manager(path):
    with RunLock(path) as lock:
        assert lock.acquire() is True
    assert RunLock(path).acquire() is True
