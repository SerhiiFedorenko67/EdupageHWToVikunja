"""Process-level run lock using flock(2) on a stable lock file.

The kernel releases a flock when its owner exits. The file itself is never
unlinked during acquisition, so all contenders keep using the same inode.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Self

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

# Per-process startup nonce; combined with the pid it identifies a holder
# generation in the diagnostic owner record.
_STARTUP_NONCE = os.urandom(8).hex()


class RunLockError(OSError):
    """Raised when the lock file cannot be created or written at all."""


class RunLock:
    """A flock-style run lock; ``acquire()`` is non-blocking.

    On platforms without fcntl (Linux is the target) this degenerates to a
    trivial always-acquire lock so callers behave identically.
    """

    def __init__(self, path: str, *, stale_timeout_s: float = 3600.0) -> None:
        self.path = path
        self.stale_timeout_s = stale_timeout_s
        self._fd: int | None = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        if fcntl is None:
            return True
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        except OSError as exc:
            raise RunLockError(
                f"cannot create lock directory for {self.path}: {exc}"
            ) from exc
        fd = self._open()
        if fd is None:
            raise RunLockError(f"cannot open lock file {self.path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # A live holder owns the flock even if the diagnostic PID in the
            # file is stale. Unlinking here could create a second lock inode.
            os.close(fd)
            return False
        self._write_owner(fd)
        if not self._owns_path(fd):
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # -- internals ---------------------------------------------------------

    def _open(self) -> int | None:
        try:
            return os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return None

    def _owns_path(self, fd: int) -> bool:
        """True when ``fd`` still refers to the inode ``self.path`` names.

        A concurrent breaker that unlinked + recreated the path in between would
        make this False, in which case the caller must not report success.
        """
        try:
            st = os.fstat(fd)
            path_st = os.stat(self.path)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == (path_st.st_dev, path_st.st_ino)

    def _write_owner(self, fd: int) -> None:
        body = (
            f"pid={os.getpid()}\n"
            f"started={datetime.now(UTC).isoformat()}\n"
            f"token={os.getpid()}:{_STARTUP_NONCE}\n"
        ).encode()
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, body)
            os.fsync(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise RunLockError(
                f"cannot write lock owner file {self.path}: {exc}"
            ) from exc
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
