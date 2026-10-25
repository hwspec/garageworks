import fcntl
import getpass
import os
import socket
import sys
from pathlib import Path

from .config import LOCK_FILE, ROOT, TMUX_STAGES, _now
from .shell import _tmux_alive, _tmux_session
from .status import load_status, record_stage_failure, save_status


class FileLock:
    """Non-blocking flock; released automatically if the process dies."""

    def __init__(self, path, world_writable=False):
        self.path = Path(path)
        self.world_writable = world_writable
        self.fd = None

    def acquire(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        except PermissionError:          # another user's board lock
            fd = os.open(self.path, os.O_RDONLY)

        if self.world_writable:
            try:
                os.fchmod(fd, 0o666)
            except OSError:
                pass

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False

        self.fd = fd
        holder = (f"pid={os.getpid()} user={getpass.getuser()} "
                  f"host={socket.gethostname()} since={_now()} "
                  f"cmd={' '.join(sys.argv)}\n")
        try:
            os.ftruncate(fd, 0)
            os.write(fd, holder.encode())
        except OSError:
            pass
        return True

    def holder(self):
        try:
            return self.path.read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _acquire_project_lock():
    lock = FileLock(LOCK_FILE)
    if not lock.acquire():
        raise RuntimeError(
            f"another gwscript is running in {ROOT} ({lock.holder()})"
        )
    return lock


def _recover_running():
    """
    Call with the project lock held.  A stage left as 'running' is either
    still alive in a detached tmux session (returned as busy) or its process
    is gone, in which case it is marked 'interrupted'.
    """
    status = load_status()
    busy = []
    changed = False

    for name, value in status.items():
        if value != "running":
            continue

        if name in TMUX_STAGES and _tmux_alive(_tmux_session(name)):
            busy.append(name)
            continue

        print(f"{name}: was running but its process is gone; "
              "marking interrupted", file=sys.stderr)
        record_stage_failure(
            name, None,
            "interrupted (process exited without recording a result)",
            interrupted=True,
        )
        status[name] = "interrupted"
        changed = True

    if changed:
        save_status(status)

    return busy


def _refuse_if_busy(busy):
    if busy:
        raise RuntimeError(
            "still running in tmux: "
            + ", ".join(f"{n} (tmux attach -t {_tmux_session(n)})" for n in busy)
        )
