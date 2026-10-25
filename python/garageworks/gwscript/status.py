import getpass
import hashlib
import json
import platform
import shutil
import socket
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from .config import (
    AVED,
    AVED_GIT_STAGES,
    CONFIG,
    OPTIONAL_STAGES,
    REQUIRED_STAGE_ORDER,
    RESULTS_FILE,
    ROOT,
    STAGE_DEPS,
    STAGE_INPUTS,
    STAGE_ORDER,
    STAGE_TOOLS,
    STATUS_FILE,
    TMUX_STAGES,
    TOOL_CMDS,
    _now,
)
from .shell import _tmux_alive, _tmux_session


def default_status():
    return {name: "pending" for name in STAGE_ORDER}


def load_status():
    status = default_status()

    if not STATUS_FILE.exists():
        return status

    for raw in STATUS_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().lower()

        if name in status:
            status[name] = value

    return status


def save_status(status):
    lines = [
        "# GarageWork build status",
        "# Values: pending, running, success, failed, interrupted",
        "# 'stale' is computed (see --status), never stored here.",
        "# This file is intentionally editable by hand.",
        "",
    ]
    for name in STAGE_ORDER:
        lines.append(f"{name}={status.get(name, 'pending')}")
    STATUS_FILE.write_text("\n".join(lines) + "\n")


def set_stage_status(name, value):
    status = load_status()
    status[name] = value
    save_status(status)


# ------------------------------------------------------------------ results


def load_stage_results():
    if not RESULTS_FILE.exists():
        return {}

    try:
        return json.loads(RESULTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_stage_results(data):
    tmp = RESULTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(RESULTS_FILE)       # atomic: an interrupt never leaves half a file


def record_stage_success(name, result, elapsed_sec, fingerprint, provenance):
    data = load_stage_results()
    entry = data.setdefault(name, {})
    now = _now()

    entry.pop("running", None)
    entry["last_attempt"] = {
        "timestamp": now,
        "success": True,
        "elapsed_sec": elapsed_sec,
        "run_id": fingerprint["run_id"],
        "provenance": provenance,
    }
    entry["last_success"] = {
        "timestamp": now,
        "elapsed_sec": elapsed_sec,
        "result": result,
        **fingerprint,
        "provenance": provenance,
    }

    save_stage_results(data)


def record_stage_failure(name, elapsed_sec, message, provenance=None,
                         interrupted=False):
    data = load_stage_results()
    entry = data.setdefault(name, {})
    entry.pop("running", None)

    entry["last_attempt"] = {
        "timestamp": _now(),
        "success": False,
        "interrupted": interrupted,
        "elapsed_sec": elapsed_sec,
        "error": message,
        "provenance": provenance,
    }

    save_stage_results(data)


def get_stage_info(name):
    return load_stage_results().get(name)


def get_stage_result(name):
    info = get_stage_info(name)
    if not info:
        return None

    last_success = info.get("last_success")
    if not last_success:
        return None

    return last_success.get("result")


def stage_succeeded(name):
    return get_stage_result(name) is not None


# ------------------------------------------------------------------ provenance


def _git(path, *args):
    if not shutil.which("git") or not (Path(path) / ".git").exists():
        return None
    r = subprocess.run(["git", "-C", str(path), *args], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.stdout.strip() if r.returncode == 0 else None


def _git_info(path):
    commit = _git(path, "rev-parse", "HEAD")
    if not commit:
        return None
    dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=no"))
    return {"commit": commit, "dirty": dirty}


_tool_cache = {}


def _tool_version(tool):
    if tool not in _tool_cache:
        cmd = TOOL_CMDS[tool]
        version = None
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, text=True, timeout=120,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
                lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
                version = lines[0] if lines else None
            except (OSError, subprocess.SubprocessError):
                pass
        _tool_cache[tool] = version
    return _tool_cache[tool]


def _gwscript_sha256():
    """Hash every .py file in the gwscript package, not just this module."""
    pkg_dir = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(pkg_dir.rglob("*.py")):
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def provenance(name):
    """What produced this stage's result: host, tools, git state, config."""
    git = {"project": _git_info(ROOT)}
    if name in AVED_GIT_STAGES and AVED.exists():
        git["aved"] = _git_info(AVED)

    return {
        "host": socket.gethostname(),
        "user": getpass.getuser(),
        "python": platform.python_version(),
        "gwscript_sha256": _gwscript_sha256(),
        "config": dict(CONFIG),
        "git": git,
        "tools": {t: _tool_version(t) for t in STAGE_TOOLS.get(name, [])},
    }


# ------------------------------------------------------------------ staleness


def _inputs_hash(name):
    spec = STAGE_INPUTS.get(name, {})
    h = hashlib.sha256()

    excluded = set()
    for pattern in spec.get("exclude", []):
        excluded.update(ROOT.glob(pattern))

    for pattern in spec.get("globs", []):
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and path not in excluded:
                h.update(str(path.relative_to(ROOT)).encode() + b"\0")
                h.update(hashlib.sha256(path.read_bytes()).digest())

    for key in spec.get("config", []):
        h.update(f"cfg:{key}={CONFIG.get(key, '')}\0".encode())

    for path in spec.get("git", []):
        h.update(f"git:{path}={_git(path, 'rev-parse', 'HEAD')}\0".encode())

    return h.hexdigest()[:16]


def _success_run_id(data, name):
    return ((data.get(name) or {}).get("last_success") or {}).get("run_id")


def _fingerprint(name):
    """Taken when a stage starts: its inputs and which upstream runs it used."""
    data = load_stage_results()
    deps = STAGE_DEPS[name]
    return {
        "run_id": uuid.uuid4().hex[:12],
        "deps": {dep: _success_run_id(data, dep) for dep in deps},
        "inputs_hash": _inputs_hash(name),
    }


def stage_state(name, status=None, data=None, memo=None):
    """
    Return (state, reason).  state is the .gwstatus value, except that a
    'success' becomes 'stale' when an upstream stage re-ran (or is no longer
    successful) or the stage's own inputs changed since it succeeded.
    Records without a run_id (hand-edited or older) are trusted as success.
    """
    status = load_status() if status is None else status
    data = load_stage_results() if data is None else data
    memo = {} if memo is None else memo

    if name in memo:
        return memo[name]

    value = status.get(name, "pending")
    state = (value, "")

    last = (data.get(name) or {}).get("last_success") or {}

    if value == "success" and "run_id" in last:
        deps = STAGE_DEPS[name]

        for dep in deps:
            dep_state, _ = stage_state(dep, status, data, memo)
            recorded = last.get("deps", {}).get(dep)
            current = _success_run_id(data, dep)

            if dep_state != "success":
                state = ("stale", f"upstream {dep} is {dep_state}")
                break
            if recorded and current and recorded != current:
                state = ("stale", f"{dep} re-ran")
                break
        else:
            if last.get("inputs_hash") not in (None, _inputs_hash(name)):
                state = ("stale", "inputs changed")

    memo[name] = state
    return state


def _fmt_secs(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def status_report():
    """Per-stage state, plus live progress for running stages."""
    status, data, memo = load_status(), load_stage_results(), {}
    report = []

    for name in STAGE_ORDER:
        state, reason = stage_state(name, status, data, memo)
        entry = data.get(name) or {}
        last = entry.get("last_success") or {}
        item = {
            "stage": name,
            "state": state,
            "reason": reason,
            "last_success": last.get("timestamp"),
            "last_elapsed_sec": last.get("elapsed_sec"),
        }

        running = entry.get("running")
        if state == "running" and running:
            since = datetime.fromisoformat(running["since"])
            item["running"] = {
                **running,
                "elapsed_sec": round((datetime.now() - since).total_seconds()),
            }
            if name in TMUX_STAGES:
                session = _tmux_session(name)
                item["running"]["tmux_session"] = session
                item["running"]["tmux_alive"] = _tmux_alive(session)

        report.append(item)

    return report


def accept_stage(name):
    """Treat a stage's current inputs as the ones it succeeded with.

    Re-fingerprints the inputs without re-running.  The run_id is kept, so
    downstream stages that used this run stay fresh.
    """
    data = load_stage_results()
    last = (data.get(name) or {}).get("last_success")
    if load_status().get(name) != "success" or not last:
        raise RuntimeError(f"{name} has no successful run to accept")

    last["inputs_hash"] = _inputs_hash(name)
    last["accepted"] = _now()
    save_stage_results(data)
    print(f"{name}: inputs accepted")


def show_status(as_json=False):
    report = status_report()

    if as_json:
        print(json.dumps({"root": str(ROOT), "stages": report}, indent=2))
        return

    for item in report:
        line = f"{item['stage']:12s} {item['state']}"
        if item["reason"]:
            line += f" ({item['reason']})"
        run_info = item.get("running")
        if run_info:
            line += f" for {_fmt_secs(run_info['elapsed_sec'])}"
            if item["last_elapsed_sec"]:
                line += f" (last took {_fmt_secs(item['last_elapsed_sec'])})"
            if run_info.get("log"):
                line += f"; log: {run_info['log']}"
            if run_info.get("tmux_session"):
                line += f"; tmux attach -t {run_info['tmux_session']}"
        print(line)


def next_runnable_stage():
    status, data, memo = load_status(), load_stage_results(), {}

    for name in REQUIRED_STAGE_ORDER:
        if stage_state(name, status, data, memo)[0] == "success":
            continue

        deps = STAGE_DEPS[name]
        required_deps = [dep for dep in deps if dep not in OPTIONAL_STAGES]

        if all(stage_state(dep, status, data, memo)[0] == "success"
               for dep in required_deps):
            return name

    return None


def show_next(as_json=False):
    name = next_runnable_stage()

    if as_json:
        deps = STAGE_DEPS[name] if name else []
        status, data, memo = load_status(), load_stage_results(), {}
        print(json.dumps({
            "next": name,
            "deps": {d: stage_state(d, status, data, memo)[0] for d in deps},
        }, indent=2))
        return

    if name is None:
        print("No pending runnable stage.")
        return

    deps = STAGE_DEPS[name]
    print(f"Next runnable stage: {name}")

    if deps:
        status, data, memo = load_status(), load_stage_results(), {}
        print(
            "Depends on: " +
            ", ".join(f"{dep} [{stage_state(dep, status, data, memo)[0]}]"
                      for dep in deps)
        )
    else:
        print("Depends on: none")
