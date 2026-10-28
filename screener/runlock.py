"""One writer at a time.

The menu's R refresh and a terminal run share one database, and both start by wiping it.
Twice on 2026-10-06 a refresh from the menu erased scores a terminal run had just written,
with nothing to say it had happened. Every command that writes to the store now takes this
lock first, and a second writer is refused with the name and age of the one already
running, instead of quietly destroying its work.

A lock left behind by a process that has since died - a crash, a closed window - is
detected by checking whether its PID is still alive, and taken over.
"""
import json
import os
import sys
import time
from datetime import datetime

from .config import DATA_DIR

LOCK_PATH = DATA_DIR / "run.lock"


class Busy(RuntimeError):
    """Another writer holds the lock."""

    def __init__(self, holder: dict):
        self.holder = holder
        started = holder.get("started", "?")
        super().__init__(f"{holder.get('what', 'another run')} is already writing to the "
                         f"store (PID {holder.get('pid')}, started {started}). Wait for it "
                         f"to finish, or close it first.")


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)         # signal 0 only checks; on POSIX it never kills
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holder() -> dict | None:
    """Who holds the lock right now, or None if nobody live does."""
    try:
        info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if _alive(int(info.get("pid") or 0)) else None


class RunLock:
    def __init__(self, what: str):
        self.what = what
        self.held = False

    def acquire(self) -> "RunLock":
        DATA_DIR.mkdir(exist_ok=True)
        me = {"pid": os.getpid(), "what": self.what,
              "started": datetime.now().strftime("%H:%M:%S on %Y-%m-%d")}
        for _ in range(2):
            try:
                # O_EXCL: creation fails if the file already exists, so two processes can
                # never both believe they won.
                fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                current = holder()
                if current and current.get("pid") != os.getpid():
                    raise Busy(current)
                # Stale (its process is gone) or our own: clear it and try once more.
                try:
                    LOCK_PATH.unlink()
                except OSError:
                    time.sleep(0.2)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(me, f)
            self.held = True
            return self
        current = holder() or {"what": "another run", "pid": "?"}
        raise Busy(current)

    def release(self):
        if not self.held:
            return
        try:
            info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                LOCK_PATH.unlink()
        except (OSError, ValueError):
            pass
        self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
