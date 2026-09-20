from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path

from ..config import Settings


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback and release file descriptors deterministically on exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(path: Path, settings: Settings) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    con = sqlite3.connect(path, timeout=settings.sqlite_busy_timeout_ms / 1000.0, isolation_level=None, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    if is_new:
        con.execute("PRAGMA auto_vacuum=INCREMENTAL")
    # WAL is persistent.  Executing ``PRAGMA journal_mode=WAL`` on every short
    # lived connection asks SQLite for an exclusive lock and caused the 29
    # service startup storm seen on the production host.  Only the creator (or
    # a database that is genuinely not WAL yet) performs the transition.
    mode = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if mode != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.execute(f"PRAGMA synchronous={settings.sqlite_sync}")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA temp_store=FILE")
    con.execute("PRAGMA cache_size=-2048")
    return con


@contextlib.contextmanager
def initialization_lock(path: Path, settings: Settings):
    """Serialize schema creation/migration across independent service PIDs.

    SQLite protects statements, but a large sequence of CREATE/ALTER/seed
    statements is not one cross-process initialization critical section.  A
    small adjacent lock file prevents every service from trying that sequence
    during supervisor startup.  Stale locks are recovered after five minutes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".init.lock")
    deadline = time.monotonic() + max(60.0, settings.sqlite_busy_timeout_ms / 1000.0 * 4.0)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode("ascii"))
        except (FileExistsError, PermissionError) as exc:
            # Windows can report sharing contention on an existing O_EXCL
            # lock as EACCES rather than EEXIST.  Treat that narrow case like
            # FileExistsError, but never hide a genuine directory/ACL failure.
            if isinstance(exc, PermissionError) and not lock.exists():
                raise
            try:
                if time.time() - lock.stat().st_mtime > 300:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"database initialization lock timed out: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def serialized_initialization(method):
    """Decorator for store ``_init_sync`` methods sharing one database."""
    def wrapped(self, *args, **kwargs):
        with initialization_lock(self.path, self.settings):
            return method(self, *args, **kwargs)
    wrapped.__name__ = getattr(method, "__name__", "_init_sync")
    wrapped.__doc__ = getattr(method, "__doc__")
    return wrapped


def transaction(con: sqlite3.Connection, immediate: bool = True) -> None:
    con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
