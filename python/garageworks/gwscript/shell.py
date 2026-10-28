import hashlib
import re
import shutil
import subprocess

from .config import ROOT, _noninteractive


def _cmd_str(cmd):
    return " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)


def run(cmd, *, cwd=ROOT, env=None, ignore_returncode=False, input=None):
    if cmd and cmd[0] == "sudo" and _noninteractive():
        # never block on a password prompt nobody can see
        cmd = ["sudo", "-n", *cmd[1:]]

    print(f"\n[{cwd}] $ {' '.join(map(str, cmd))}")
    result = subprocess.run(cmd, cwd=cwd, env=env, check=False, text=True,
                            input=input,
                            stdout=subprocess.DEVNULL if input else None)

    if result.returncode != 0:
        if ignore_returncode:
            print(
                f"WARNING: ignoring exit code {result.returncode}: "
                f"{' '.join(map(str, cmd))}"
            )
        else:
            raise subprocess.CalledProcessError(result.returncode, cmd)

    return result


def capture(cmd, *, cwd=ROOT, timeout=None):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return result.stdout.strip()


def _tmux_session(name):
    """Per-project session name, so two checkouts never collide."""
    tag = hashlib.sha1(str(ROOT).encode()).hexdigest()[:6]
    proj = re.sub(r"[^0-9A-Za-z_-]", "_", ROOT.name)
    return f"gw-{proj}-{tag}-{name}"


def _tmux_alive(session):
    if not shutil.which("tmux"):
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
