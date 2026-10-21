#!/usr/bin/env python3
#
# Copyright (c) 2026, UChicago Argonne, LLC.
# License: See LICENSE in the project top-level directory.
# Main author: Kazutomo Yoshii <kazutomo@anl.gov>
#
# gwscript: GarageWorks build/test driver (RTL gen -> cocotb sim -> V80 FPGA)
#
# Dependencies
#   Python  : 3.8+ on Linux, standard library only (venv/pip for the bridge stage)
#
#   Simulation stages (prereq, repo, bridge, rtlgen, cocotb):
#     git        submodule init (repo)
#     make       garageworks setup, cocotb test targets
#     Verilator  5.044 recommended (cocotb simulator)
#     GCC        11.1.0 recommended (Verilator C++ build)
#     Java + sbt Chisel RTL generation (rtlgen)
#     cocotb     installed into garageworks/.venv by the bridge stage
#
#   FPGA stages (--fpga: fpgatiming, aved, buildhw, programhw, testhw):
#     Vivado/Vitis 2025.1   synthesis, timing sweep, AVED hardware build
#     gcc-arm-none-eabi     AVED firmware (from Vitis gnu/armr5)
#     tmux                  runs fpgatiming and buildhw in a detached session
#     AMD Alveo V80 + AVED  target board; AVED-gw is cloned by the aved stage
#     ami_tool              AVED device tool (default /usr/local/bin/ami_tool)
#     lspci (pciutils)      PCIe device presence check after programming
#     sudo                  PCIe rescan and ami_tool reload in programhw
#
#   MCP mode (--mcp): the `mcp` package (pip install mcp); Python 3.10+
#   Non-interactive runs (MCP, tmux) need passwordless sudo: see --sudoers
#
#   Environment for FPGA stages: XILINXD_LICENSE_FILE, Vitis settings64.sh
#   sourced, and the armr5 toolchain on PATH (see README).

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

def _early_chdir(argv):
    """Honor -C/--root before the module reads .gwconfig from the cwd."""
    for i, arg in enumerate(argv):
        if arg in ("-C", "--root") and i + 1 < len(argv):
            os.chdir(argv[i + 1])
            return
        if arg.startswith("--root="):
            os.chdir(arg.split("=", 1)[1])
            return


_early_chdir(sys.argv[1:])

ROOT = Path.cwd().resolve()
CONFIG_FILE = ROOT / ".gwconfig"


def load_config():
    config = {}

    if not CONFIG_FILE.exists():
        raise RuntimeError(
            f"Missing config file: {CONFIG_FILE}\n"
            "Create .gwconfig in the project root with:\n"
            "  name=<top-level Chisel module>\n"
            "  package=<Scala package of that module>\n"
            "  tests=<comma-separated test targets>\n"
            "  tests_dir=<testbench directory> (optional, default: gwtests)"
        )

    for raw in CONFIG_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()

    required = ["name", "package"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RuntimeError(
            "Missing required .gwconfig keys: " + ", ".join(missing)
        )

    return config


CONFIG = load_config()
NAME = CONFIG["name"]
PACKAGE = CONFIG["package"]
TESTS = [
    item.strip()
    for item in CONFIG.get("tests", "").split(",")
    if item.strip()
]

AVED_REPO = CONFIG.get("aved_repo", "https://github.com/hwspec/AVED-gw.git")
# testbench directory (Makefile with <test> and hw_<test> targets)
TB_DIR_NAME = CONFIG.get("tests_dir", "gwtests")
TB_DIR = ROOT / TB_DIR_NAME

AVED = ROOT / "AVED-gw"
AVED_HW = AVED / "hw/amd_v80_gen5x8_25.1"
GW_RTL = AVED_HW / "src/rtl/garageworks"   # AVED-gw expects top module `wrapper`
BDF = CONFIG.get("bdf", "b1:00.0")
AMI_TOOL = CONFIG.get("ami_tool", "/usr/local/bin/ami_tool")
GENERATED = ROOT / "generated" / NAME
VENV = ROOT / "garageworks/.venv"

STATUS_FILE = ROOT / ".gwstatus"
RESULTS_FILE = ROOT / ".gwstage-results.json"
LOG_DIR = ROOT / ".gwlogs"

completed = set()
stage_results = {}
stage_times = {}

TMUX_STAGES = {"fpgatiming", "buildhw"}

FPGA_STAGES = {
    "fpgatiming",
    "aved",
    "buildhw",
    "programhw",
    "testhw",
}


if "fpgatest" in CONFIG:
    print("WARNING: 'fpgatest' in .gwconfig is ignored; use --fpga instead.",
          file=sys.stderr)

# Set from --fpga in main(); naming an FPGA stage explicitly also enables it.
FPGA_ENABLED = False
FPGA_SKIP_REASON = "FPGA stages disabled (use --fpga)"

CLEAN_FILES = [
    STATUS_FILE,
    RESULTS_FILE,
    ROOT / "aved-compile.log",
    TB_DIR / "output.log",
]
CLEAN_DIRS = [LOG_DIR, ROOT / ".gwjobs"]

STAGE_ORDER = [
    "prereq",
    "repo",
    "bridge",
    "rtlgen",
    "cocotb",
    "fpgatiming",
    "aved",
    "buildhw",
    "programhw",
    "testhw",
]

REQUIRED_STAGE_ORDER = [
    "prereq",
    "repo",
    "bridge",
    "rtlgen",
    "cocotb",
    "aved",
    "buildhw",
    "programhw",
    "testhw",
]

OPTIONAL_STAGES = {"fpgatiming"}

# sudo may prompt for a password, which would hang invisibly inside a detached
# tmux session.  Keep sudo stages in the foreground.
SUDO_STAGES = {"programhw"}
assert not (SUDO_STAGES & TMUX_STAGES), "sudo stages must not run inside tmux"

# stages that touch the physical board; serialized across users/projects
BOARD_STAGES = {"programhw", "testhw"}

LOCK_FILE = ROOT / ".gwlock"
JOBS_DIR = ROOT / ".gwjobs"          # MCP background jobs (log + rc + meta)

TEE = shutil.which("tee") or "/usr/bin/tee"
# commands programhw runs with sudo; must be NOPASSWD for non-interactive runs
SUDO_COMMANDS = [
    [AMI_TOOL, "reload", "-t", "sbr", "-d", BDF],
    [TEE, "/sys/bus/pci/rescan"],
]
BOARD_LOCK_FILE = Path(tempfile.gettempdir()) / (
    "gw-board-" + re.sub(r"[^0-9A-Za-z]", "_", BDF) + ".lock"
)

# archived logs kept per tmux stage in .gwlogs (current log + LOG_KEEP old)
LOG_KEEP = int(CONFIG.get("log_keep", "5"))

# Inputs that make a successful stage stale when they change (besides an
# upstream stage re-running).  globs are relative to the project root.
STAGE_INPUTS = {
    "rtlgen": {
        "globs": ["src/**/*.scala", "build.sbt", "project/*.sbt",
                  "project/build.properties"],
        "config": ["name", "package"],
    },
    "cocotb": {
        "globs": [f"{TB_DIR_NAME}/{g}" for g in
                  ("*.py", "Makefile", "*.s", "*.c", "*.tbspec")],
        "exclude": [f"{TB_DIR_NAME}/fpga_*.py"],   # generated by make hw_<test>
        "config": ["tests"],
    },
    "fpgatiming": {"git": [AVED]},
    # not the AVED-gw commit: the clone doesn't exist yet when aved starts
    "aved": {"config": ["aved_repo"]},
    "buildhw": {"git": [AVED]},
    "programhw": {"config": ["bdf"]},
    "testhw": {
        "globs": [f"{TB_DIR_NAME}/*.py", f"{TB_DIR_NAME}/Makefile"],
        "exclude": [f"{TB_DIR_NAME}/fpga_*.py"],
        "config": ["tests", "bdf"],
    },
}

# tool versions recorded in each stage's provenance
TOOL_CMDS = {
    "verilator": ["verilator", "--version"],
    "gcc": ["gcc", "-dumpfullversion"],
    "java": ["java", "-version"],
    "sbt": ["sbt", "--script-version"],
    "vivado": ["vivado", "-version"],
}
STAGE_TOOLS = {
    "prereq": ["verilator", "gcc"],
    "rtlgen": ["java", "sbt"],
    "cocotb": ["verilator", "gcc"],
    "fpgatiming": ["vivado"],
    "buildhw": ["vivado"],
    "programhw": ["vivado"],
}
AVED_GIT_STAGES = {"fpgatiming", "aved", "buildhw", "programhw", "testhw"}


class StageInterrupted(BaseException):
    """Raised by the SIGTERM/SIGHUP handler (tmux kill-session sends SIGHUP)."""


def _on_signal(signum, frame):
    raise StageInterrupted(signal.Signals(signum).name)


def _cmd_str(cmd):
    return " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)


def _noninteractive():
    """True under MCP, inside tmux, or without a terminal to prompt on."""
    return bool(os.environ.get("GW_IN_TMUX") or os.environ.get("GW_NONINTERACTIVE")
                or not sys.stdin.isatty())


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


def capture(cmd, *, cwd=ROOT):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def venv_env():
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(VENV)
    env["PATH"] = f"{VENV / 'bin'}:{env['PATH']}"
    return env


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


def _now():
    return datetime.now().isoformat(timespec="seconds")


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


def provenance(name):
    """What produced this stage's result: host, tools, git state, config."""
    git = {"project": _git_info(ROOT)}
    if name in AVED_GIT_STAGES and AVED.exists():
        git["aved"] = _git_info(AVED)

    return {
        "host": socket.gethostname(),
        "user": getpass.getuser(),
        "python": platform.python_version(),
        "gwscript_sha256": hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest()[:12],
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
    deps, _ = STAGES[name]
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
        deps, _ = STAGES[name]

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

        deps, _ = STAGES[name]
        required_deps = [dep for dep in deps if dep not in OPTIONAL_STAGES]

        if all(stage_state(dep, status, data, memo)[0] == "success"
               for dep in required_deps):
            return name

    return None


def show_next(as_json=False):
    name = next_runnable_stage()

    if as_json:
        deps = STAGES[name][0] if name else []
        status, data, memo = load_status(), load_stage_results(), {}
        print(json.dumps({
            "next": name,
            "deps": {d: stage_state(d, status, data, memo)[0] for d in deps},
        }, indent=2))
        return

    if name is None:
        print("No pending runnable stage.")
        return

    deps, _ = STAGES[name]
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


# ------------------------------------------------------------------ locking


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


def prereq():
    verilator = capture(["verilator", "--version"])
    print(f"Verilator: {verilator}")

    m = re.search(r"(\d+\.\d+)", verilator)
    if not m or m.group(1) != "5.044":
        print("WARNING: Verilator 5.044 is recommended.")

    gcc = capture(["gcc", "-dumpfullversion"])
    print(f"GCC: {gcc}")

    if gcc != "11.1.0":
        print("WARNING: GCC 11.1.0 is recommended (non-strict check).")

    return {"verilator": verilator, "gcc": gcc}


def repo():
    status = capture(["git", "submodule", "status"])
    uninitialized = any(
        line.startswith("-")
        for line in status.splitlines()
        if line.strip()
    )

    if uninitialized:
        run(["git", "submodule", "update", "--init"])
    else:
        print("Git submodules already initialized.")

    return {"initialized": True}


def bridge():
    run(["make", "-C", "garageworks", "setup"])
    return {"venv": str(VENV)}


def rtlgen():
    run(["sbt", f"runMain {PACKAGE}.{NAME}"])

    if not GENERATED.is_dir():
        raise RuntimeError(
            f"RTL generation succeeded but {GENERATED} does not exist"
        )

    return {"generated_dir": str(GENERATED)}


def cocotb():
    if not TESTS:
        raise RuntimeError("No tests defined in .gwconfig (example: tests=basic,loop)")

    env = venv_env()
    run(["make", "-C", str(TB_DIR), "clean"], env=env)

    for testname in TESTS:
        print(f"\nRunning cocotb test target: {testname}")
        run(["make", "-C", str(TB_DIR), testname], env=env)

    return {
        "tests_dir": str(TB_DIR),
        "tests": TESTS,
    }


def _set_xdc_period(xdc_path, period_ns):
    text = xdc_path.read_text()

    pattern = re.compile(
        r"(create_clock\b[^\n]*?-period\s+)([0-9]*\.?[0-9]+)",
        re.IGNORECASE,
    )

    new_text, count = pattern.subn(
        lambda m: f"{m.group(1)}{period_ns:.4f}",
        text,
        count=1,
    )

    if count != 1:
        raise RuntimeError(
            f"Could not uniquely update create_clock -period in {xdc_path}"
        )

    xdc_path.write_text(new_text)


def _find_timing_report(workdir):
    candidates = list(workdir.rglob("*timing*.rpt"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _parse_wns_from_text(text):
    patterns = [
        r"\bWNS(?:\(ns\))?\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
        r"\bWNS\s+TNS\b.*?\n\s*(-?\d+(?:\.\d+)?)",
    ]

    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return float(m.group(1))

    return None


def _run_timing_trial(period_ns):
    workdir = GENERATED
    xdc = workdir / "constraints.xdc"

    if not xdc.exists():
        raise RuntimeError(f"Missing XDC file: {xdc}")

    _set_xdc_period(xdc, period_ns)

    LOG_DIR.mkdir(exist_ok=True)
    safe = str(period_ns).replace(".", "p")
    log_path = LOG_DIR / f"fpgatiming-{safe}ns.log"

    print(f"\nTrying FPGA period {period_ns:.4f} ns")
    print(f"Log: {log_path}")

    with log_path.open("w") as logfile:
        proc = subprocess.Popen(
            ["./compile.sh"],
            cwd=workdir,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        assert proc.stdout is not None
        output_lines = []

        for line in proc.stdout:
            sys.stdout.write(line)
            logfile.write(line)
            output_lines.append(line)

        rc = proc.wait()

    output = "".join(output_lines)

    report = _find_timing_report(workdir)
    report_text = report.read_text(errors="replace") if report else ""

    wns = _parse_wns_from_text(report_text) if report_text else None
    if wns is None:
        wns = _parse_wns_from_text(output)

    compile_ok = (rc == 0)
    timing_ok = compile_ok and (wns is not None) and (wns >= 0.0)

    return {
        "period_ns": period_ns,
        "compile_returncode": rc,
        "wns_ns": wns,
        "timing_ok": timing_ok,
        "report": str(report) if report else None,
        "log": str(log_path),
    }


def fpgatiming(start_period=2.0, max_period=10.0, resolution=0.05):
    coarse_periods = [2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0]
    coarse_periods = [p for p in coarse_periods if p >= start_period]

    if start_period not in coarse_periods:
        coarse_periods.insert(0, start_period)

    trials = []
    last_fail = None
    first_pass = None

    for period in coarse_periods:
        if period > max_period:
            break

        trial = _run_timing_trial(period)
        trials.append(trial)

        state = "PASS" if trial["timing_ok"] else "FAIL"
        print(
            f"Timing {state}: period={period:.4f} ns "
            f"WNS={trial['wns_ns']}"
        )

        if trial["timing_ok"]:
            first_pass = period
            break

        last_fail = period

    if first_pass is None:
        raise RuntimeError(
            f"No passing FPGA timing found through {max_period:.3f} ns"
        )

    best_period = first_pass
    best_trial = trials[-1]

    if last_fail is not None:
        low = last_fail
        high = first_pass

        while (high - low) > resolution:
            mid = (low + high) / 2.0
            trial = _run_timing_trial(mid)
            trials.append(trial)

            state = "PASS" if trial["timing_ok"] else "FAIL"
            print(
                f"Timing {state}: period={mid:.4f} ns "
                f"WNS={trial['wns_ns']}"
            )

            if trial["timing_ok"]:
                high = mid
                best_period = mid
                best_trial = trial
            else:
                low = mid

    result = {
        "period_ns": best_period,
        "frequency_mhz": 1000.0 / best_period,
        "wns_ns": best_trial["wns_ns"],
        "report": best_trial["report"],
        "log": best_trial["log"],
        "resolution_ns": resolution,
        "trials": trials,
    }

    print(
        f"\nLowest passing period: {best_period:.4f} ns "
        f"({result['frequency_mhz']:.2f} MHz)"
    )

    return result


def aved():
    if AVED.exists():
        if not (AVED / ".git").is_dir():
            raise RuntimeError(
                f"{AVED} exists but does not appear to be a git repository"
            )
        print(f"AVED repository already exists: {AVED}")
    else:
        run(["git", "clone", AVED_REPO, str(AVED)])

        if not (AVED / ".git").is_dir():
            raise RuntimeError("AVED clone did not complete successfully")

    os.environ["AVED"] = str(AVED)
    print(f"AVED={AVED}")

    # builds libami.so + libvamp.so/pyaved.py (sw/vamp/build)
    run(["make", "sw"], cwd=AVED)

    return {"aved_dir": str(AVED)}


def _paramfn():
    """params.json if present, else the single generated *.json."""
    p = GW_RTL / "params.json"
    if p.is_file():
        return p
    jsons = sorted(GW_RTL.glob("*.json"))
    if len(jsons) != 1:
        raise RuntimeError(f"Cannot pick params JSON in {GW_RTL}: {jsons}")
    return jsons[0]


def _copy_useracc():
    """Copy generated user-accelerator RTL/JSON into AVED-gw."""
    GW_RTL.mkdir(parents=True, exist_ok=True)

    files = []
    for pattern in ("*.v", "*.sv", "*.json"):
        files.extend(GENERATED.glob(pattern))

    if not files:
        raise RuntimeError(f"No generated RTL/JSON files found in {GENERATED}")

    # remove previous design so stale RTL/JSON never leaks into the build
    for pattern in ("*.v", "*.sv", "*.json"):
        for old in GW_RTL.glob(pattern):
            old.unlink()

    copied = []

    for src in files:
        dst = GW_RTL / src.name
        print(f"copy {src} -> {dst}")
        shutil.copy2(src, dst)
        copied.append(str(dst))

    # GarageWorks emits user_accel_bd_wrapper.v; AVED-gw's BD references `wrapper`
    gw_wrapper = GW_RTL / "user_accel_bd_wrapper.v"
    wrapper = GW_RTL / "wrapper.v"
    if gw_wrapper.is_file():
        text = gw_wrapper.read_text()
        text, n = re.subn(r"^module\s+user_accel_bd_wrapper\b", "module wrapper",
                          text, count=1, flags=re.MULTILINE)
        if n != 1:
            raise RuntimeError(f"Could not rename module in {gw_wrapper}")
        wrapper.write_text(text)
        gw_wrapper.unlink()
        copied = [c for c in copied if c != str(gw_wrapper)] + [str(wrapper)]
        print(f"rename {gw_wrapper.name} -> {wrapper.name} (module wrapper)")

    if not wrapper.is_file():
        raise RuntimeError(f"Missing {wrapper} (top module must be `wrapper`)")

    return {"copied": copied, "paramfn": str(_paramfn())}


def buildhw():
    useracc = _copy_useracc()

    log_path = ROOT / "aved-compile.log"
    build_dir = AVED_HW / "build"

    if build_dir.exists():
        print(f"Removing previous hardware build directory: {build_dir}")
        shutil.rmtree(build_dir)

    print(f"\n[{AVED_HW}] $ ./build_all.sh")
    print(f"Logging output to {log_path}")

    with log_path.open("w") as logfile:
        proc = subprocess.Popen(
            ["./build_all.sh"],
            cwd=AVED_HW,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        assert proc.stdout is not None

        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                logfile.write(line)
        except BaseException:
            proc.terminate()            # don't leave Vivado running
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise

        rc = proc.wait()

    if rc != 0:
        raise subprocess.CalledProcessError(rc, ["./build_all.sh"])

    print("Hardware build completed successfully.")
    return {**useracc, "log": str(log_path)}


def _device_present():
    r = subprocess.run(["lspci", "-s", BDF], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return bool(r.stdout.strip())


def _pci_rescan():
    run(["sudo", TEE, "/sys/bus/pci/rescan"], input="1\n")
    time.sleep(2)


def programhw():
    prog_result = run(
        ["make", "prog"],
        cwd=AVED,
        ignore_returncode=True,
    )

    # JTAG programming drops the PCIe link; the kernel may remove the device
    if not _device_present():
        _pci_rescan()

    if _device_present():
        # sbr removes the device itself and rescans; re-check afterwards.
        # Must succeed: without the reload the new bitstream isn't usable,
        # even though the device may still show up on PCIe.
        run(["sudo", AMI_TOOL, "reload", "-t", "sbr", "-d", BDF])
        time.sleep(2)

    if not _device_present():
        _pci_rescan()

    if not _device_present():
        raise RuntimeError(f"{BDF} not on PCIe after programming; power cycle may be needed")

    run([AMI_TOOL, "overview"], ignore_returncode=True)

    return {
        "make_prog_returncode": prog_result.returncode,
        "bdf": BDF,
    }


def testhw(testname=None):
    env = venv_env()
    env["AVED"] = str(AVED)

    vamp_build = AVED / "sw/vamp/build"   # libvamp.so + pyaved.py
    ami_build = AVED / "sw/AMI/api/build"  # libami.so

    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{old_pythonpath}:{vamp_build}" if old_pythonpath else str(vamp_build)
    )

    old_ldpath = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{vamp_build}:{ami_build}" + (
        f":{old_ldpath}" if old_ldpath else ""
    )

    env["PARAMFN"] = str(_paramfn())

    targets = [testname] if testname else TESTS
    if not targets:
        raise RuntimeError("No tests defined in .gwconfig (example: tests=basic,loop)")

    results = []

    for target in targets:
        print(f"\nRunning FPGA test target: {target}")
        run(
            ["make", f"hw_{target}"],
            cwd=TB_DIR,
            env=env,
        )

        output_log = TB_DIR / "output.log"
        if not output_log.is_file():
            raise RuntimeError(f"Expected output file not found: {output_log}")

        print(f"\nFPGA output ({target}): {output_log}")
        print("-" * 72)

        lines = output_log.read_text(errors="replace").splitlines()
        for line in lines[-30:]:
            print(line)

        results.append({
            "testname": target,
            "output_log": str(output_log),
        })

    return {"tests": results}


STAGES = {
    "prereq":     ([], prereq),
    "repo":       (["prereq"], repo),
    "bridge":     (["repo"], bridge),
    "rtlgen":     (["repo"], rtlgen),
    "cocotb":     (["rtlgen", "bridge"], cocotb),
    "fpgatiming": (["rtlgen"], fpgatiming),
    "aved":       (["prereq"], aved),
    "buildhw":    (["rtlgen", "aved"], buildhw),
    "programhw":  (["buildhw"], programhw),
    "testhw":     (["programhw"], testhw),
}


def _latest_attempt_succeeded(name):
    info = get_stage_info(name)
    if not info:
        return False

    last_attempt = info.get("last_attempt")
    return bool(last_attempt and last_attempt.get("success"))


def _execute_stage(name, *args):
    _, func = STAGES[name]

    print()
    print("=" * 72)
    print(f"STAGE: {name}")
    print("=" * 72)

    # Non-interactive runs (MCP, tmux) can't answer a password prompt, so
    # check NOPASSWD up front (sudo -n never prompts).  Interactive runs
    # just run the sudo commands; sudo itself prompts only if it must.
    if name in SUDO_STAGES and _noninteractive():
        _check_nopasswd_sudo()

    board_lock = None
    if name in BOARD_STAGES:
        board_lock = FileLock(BOARD_LOCK_FILE, world_writable=True)
        if not board_lock.acquire():
            raise RuntimeError(
                f"board {BDF} is in use ({board_lock.holder()})"
            )

    fingerprint = _fingerprint(name)
    prov = provenance(name)

    data = load_stage_results()
    log = os.environ.get("GW_LOG")
    if os.environ.get("GW_IN_TMUX"):
        log = str(LOG_DIR / f"{name}.log")
    data.setdefault(name, {})["running"] = {
        "since": _now(), "pid": os.getpid(), "host": socket.gethostname(),
        "log": log,
    }
    save_stage_results(data)

    set_stage_status(name, "running")
    start = time.monotonic()

    try:
        result = func(*args)

    except (KeyboardInterrupt, StageInterrupted) as e:
        elapsed = time.monotonic() - start
        stage_times[name] = elapsed
        why = "SIGINT" if isinstance(e, KeyboardInterrupt) else str(e)

        record_stage_failure(name, elapsed, f"interrupted ({why})", prov,
                             interrupted=True)
        set_stage_status(name, "interrupted")
        raise

    except Exception as e:
        elapsed = time.monotonic() - start
        stage_times[name] = elapsed

        record_stage_failure(name, elapsed, str(e), prov)
        set_stage_status(name, "failed")
        raise

    finally:
        if board_lock:
            board_lock.release()

    elapsed = time.monotonic() - start
    stage_results[name] = result
    stage_times[name] = elapsed

    record_stage_success(name, result, elapsed, fingerprint, prov)
    set_stage_status(name, "success")
    return result


def sudoers_line():
    user = getpass.getuser()
    cmds = ", ".join([AMI_TOOL, f"{TEE} /sys/bus/pci/rescan"])
    return f"{user} ALL=(root) NOPASSWD: {cmds}"


def _nopasswd_paths():
    """
    Command paths the user may run with NOPASSWD, from `sudo -n -l`.
    'ALL' means every command.  Empty if sudo can't list without a password.
    """
    r = subprocess.run(["sudo", "-n", "-l"], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return set()

    paths = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("(") or ")" not in line:
            continue                     # only rule lines: "(root) NOPASSWD: ..."
        nopasswd = False
        for item in line.split(")", 1)[1].split(","):
            item = item.strip()
            # tags carry over to the following commands until changed
            while ":" in item and item.split(":", 1)[0].isupper():
                tag, item = item.split(":", 1)
                item = item.strip()
                if tag == "NOPASSWD":
                    nopasswd = True
                elif tag == "PASSWD":
                    nopasswd = False
            if nopasswd and item:
                paths.add(item.split()[0])
    return paths


def _missing_nopasswd():
    """
    SUDO_COMMANDS that sudo would not run without a password.

    `sudo -n -l <cmd>` alone isn't enough: it succeeds whenever the command is
    *allowed*, even if running it would still ask for a password.  So the
    command must also appear under a NOPASSWD tag.
    """
    nopasswd = _nopasswd_paths()
    missing = []
    for cmd in SUDO_COMMANDS:
        allowed = subprocess.run(["sudo", "-n", "-l", *cmd],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).returncode == 0
        if not (allowed and ("ALL" in nopasswd or cmd[0] in nopasswd)):
            missing.append(" ".join(cmd))
    return missing


def _check_nopasswd_sudo():
    """Fail fast (instead of hanging) if sudo would need a password."""
    missing = _missing_nopasswd()

    if missing:
        raise RuntimeError(
            "passwordless sudo is required for non-interactive runs but is not "
            "configured for:\n  " + "\n  ".join(missing) + "\n"
            "Add with `sudo visudo -f /etc/sudoers.d/gwscript`:\n  "
            + sudoers_line()
        )


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


def _rotate_log(log_path, keep=LOG_KEEP):
    """Archive log_path as <stem>.<mtime>.log and keep only the newest `keep`."""
    if log_path.exists():
        stamp = datetime.fromtimestamp(
            log_path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
        log_path.rename(log_path.with_name(f"{log_path.stem}.{stamp}.log"))

    archived = sorted(log_path.parent.glob(f"{log_path.stem}.*.log"))
    for old in archived[:max(len(archived) - keep, 0)]:
        old.unlink()


def _follow_log(log_path, session, poll=0.5):
    """Stream log_path to stdout (like tail -f) until the tmux session exits."""
    pos = 0

    def drain():
        nonlocal pos
        try:
            size = log_path.stat().st_size
        except FileNotFoundError:
            return
        if size < pos:          # file was truncated/recreated
            pos = 0
        if size == pos:
            return
        with log_path.open("r", errors="replace") as f:
            f.seek(pos)
            sys.stdout.write(f.read())
            pos = f.tell()
        sys.stdout.flush()

    while _tmux_alive(session):
        drain()
        time.sleep(poll)

    drain()                     # pick up anything written right before exit


def _run_stage_in_tmux(name):
    import shlex

    if not shutil.which("tmux"):
        raise RuntimeError(f"{name} runs in tmux, but tmux is not installed")

    LOG_DIR.mkdir(exist_ok=True)

    session = _tmux_session(name)
    log_path = LOG_DIR / f"{name}.log"
    # re-invoke whatever launched us (script, wrapper or console entry point)
    script = Path(sys.argv[0]).resolve()

    if _tmux_alive(session):
        raise RuntimeError(f"tmux session already exists: {session}")

    _rotate_log(log_path)

    # A tmux server started earlier keeps its own environment, so hand our
    # environment (Vivado PATH, license, ...) to the child explicitly.
    env_file = LOG_DIR / f"{name}.env.json"
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict(os.environ), f)

    # GW_IN_TMUX: the child skips locking (we hold it) and uses sudo -n
    # -u: unbuffered, so output reaches the log (and this terminal) live
    cmd = (
        "GW_IN_TMUX=1 "
        f"{shlex.quote(sys.executable)} -u "
        f"{shlex.quote(str(script))} "
        f"--direct-stage {shlex.quote(name)} "
        f"--env-file {shlex.quote(str(env_file))} "
        f"2>&1 | tee {shlex.quote(str(log_path))}"
    )

    run([
        "tmux",
        "new-session",
        "-d",
        "-s", session,
        "-c", str(ROOT),
        cmd,
    ])

    print(f"{name}: running in tmux session '{session}'")
    print(f"log: {log_path}")
    print(f"Ctrl-C stops following only; reattach with: tmux attach -t {session}")
    sys.stdout.flush()

    try:
        _follow_log(log_path, session)
    except KeyboardInterrupt:
        # Ctrl-C detaches; the build keeps going
        print(
            f"\n{name}: still running in tmux session '{session}' "
            f"(tmux attach -t {session}; log: {log_path})"
        )
        sys.exit(130)
    except StageInterrupted:
        # SIGTERM/SIGHUP (e.g. MCP cancel) stops the build itself
        print(f"\n{name}: cancelling tmux session '{session}'", file=sys.stderr)
        subprocess.run(["tmux", "kill-session", "-t", session],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):          # let the child record 'interrupted'
            if load_status().get(name) != "running":
                break
            time.sleep(0.5)
        if load_status().get(name) == "running":
            record_stage_failure(name, None, "interrupted (cancelled)",
                                 interrupted=True)
            set_stage_status(name, "interrupted")
        raise

    state = load_status().get(name)

    if state == "running":       # child died without recording (e.g. kill -9)
        record_stage_failure(
            name, None, "interrupted (tmux session ended without a result)",
            interrupted=True,
        )
        set_stage_status(name, "interrupted")
        state = "interrupted"

    if state == "interrupted":
        raise RuntimeError(f"{name} was interrupted; see {log_path}")

    if not _latest_attempt_succeeded(name):
        raise RuntimeError(f"{name} failed; see {log_path}")

    result = get_stage_result(name)
    stage_results[name] = result

    info = get_stage_info(name) or {}
    last_success = info.get("last_success", {})

    if "elapsed_sec" in last_success:
        stage_times[name] = last_success["elapsed_sec"]

    return result


def _check_stage_args(name, stage_args):
    if name == "testhw":
        if len(stage_args) > 1:
            raise RuntimeError("testhw accepts at most one test name")
    elif stage_args:
        raise RuntimeError(f"Stage {name} does not accept positional arguments")


def run_named_stage(name, stage_args=None):
    """Run exactly one named stage; do not run dependencies."""
    stage_args = stage_args or []

    if name in FPGA_STAGES and not FPGA_ENABLED:
        print(f"{name}: skipped ({FPGA_SKIP_REASON})")
        return {
            "skipped": True,
            "reason": FPGA_SKIP_REASON,
        }

    _check_stage_args(name, stage_args)

    if name in TMUX_STAGES:
        return _run_stage_in_tmux(name)

    return _execute_stage(name, *stage_args)


def plan_workflow(start_stage=None):
    """
    Return the resume plan as [(stage, action, reason)], action 'run'/'skip'.

    With start_stage, force-run that stage and every later required stage.
    Otherwise run every required stage that is not (fresh) success: pending,
    failed, interrupted or stale.  fpgatiming runs only when selected.
    """
    if start_stage == "fpgatiming":
        return [("fpgatiming", "run", "requested")]

    order = REQUIRED_STAGE_ORDER
    forced = start_stage is not None

    if forced and start_stage not in order:
        raise RuntimeError(f"Unknown required resume stage: {start_stage}")

    names = order[order.index(start_stage):] if forced else order
    status, data, memo = load_status(), load_stage_results(), {}
    plan, will_run = [], set()

    for name in names:
        if name in FPGA_STAGES and not FPGA_ENABLED:
            plan.append((name, "skip", FPGA_SKIP_REASON))
            continue

        state, reason = stage_state(name, status, data, memo)

        if forced:
            why = "forced by --resume"
        elif state == "success":
            continue
        else:
            deps, _ = STAGES[name]
            missing = [
                dep for dep in deps
                if dep not in OPTIONAL_STAGES
                and dep not in will_run
                and stage_state(dep, status, data, memo)[0] != "success"
                and not (dep in FPGA_STAGES and not FPGA_ENABLED)
            ]
            if missing:
                raise RuntimeError(
                    f"Cannot resume {name}: dependencies not successful: "
                    + ", ".join(missing)
                )
            why = state + (f": {reason}" if reason else "")

        plan.append((name, "run", why))
        will_run.add(name)

    return plan


def resume_workflow(start_stage=None):
    plan = plan_workflow(start_stage)

    if not any(action == "run" for _, action, _ in plan):
        for name, _, reason in plan:
            print(f"{name}: skipped ({reason})")
        print("All required stages are complete.")
        return

    for name, action, reason in plan:
        if action == "skip":
            print(f"{name}: skipped ({reason})")
            continue

        print(f"\nResuming stage: {name} ({reason})")
        run_named_stage(name)


def dry_run(stage=None, stage_args=None, resume=None, as_json=False):
    """Show what would run, and why, without executing anything."""
    if stage is not None:
        _check_stage_args(stage, stage_args or [])
        status, data, memo = load_status(), load_stage_results(), {}
        deps, _ = STAGES[stage]
        not_ok = [d for d in deps
                  if stage_state(d, status, data, memo)[0] != "success"]
        plan = [(stage, "run", "requested" + (
            f"; WARNING deps not success: {', '.join(not_ok)}" if not_ok else ""
        ))]
        if stage in FPGA_STAGES and not FPGA_ENABLED:
            plan = [(stage, "skip", FPGA_SKIP_REASON)]
    else:
        plan = plan_workflow(resume)

    def tags_of(name):
        return [t for t, s in (("tmux", TMUX_STAGES), ("sudo", SUDO_STAGES),
                               ("board", BOARD_STAGES)) if name in s]

    if as_json:
        print(json.dumps({"plan": [
            {"stage": n, "action": a, "reason": r, "tags": tags_of(n)}
            for n, a, r in plan
        ]}, indent=2))
        return

    print("DRY RUN (nothing is executed)")

    if not any(action == "run" for _, action, _ in plan):
        print("Nothing to run: all required stages are complete.")

    for name, action, reason in plan:
        tags = tags_of(name)
        tag = f"  [{', '.join(tags)}]" if tags and action == "run" else ""
        print(f"  {action:4s}  {name:12s} {reason}{tag}")


def print_timing_summary(total_elapsed):
    if not stage_times:
        return

    print()
    print("=" * 72)
    print("STAGE TIMING")
    print("=" * 72)

    for name in STAGE_ORDER:
        if name in stage_times:
            print(f"{name:12s} {stage_times[name]:10.1f} s")

    print("-" * 24)
    print(f"{'Total':12s} {total_elapsed:10.1f} s")


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        print(f"rm -rf {path}")
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        print(f"rm {path}")
        path.unlink()


def clean(all_=False):
    """Remove state/log files; with all_, also remove the AVED-gw clone."""
    for path in CLEAN_FILES + CLEAN_DIRS:
        _remove(path)

    if all_:
        _remove(AVED)

    print("Clean done." if not all_ else "Clean-all done.")


# ====================================================================== MCP
#
# `gwscript --mcp` serves the same workflow to an MCP client (Claude Code,
# Claude Desktop, ...).  Every tool shells out to this script, so the MCP
# server and the command line share one implementation, one lock and one
# set of state files.  Long stages run as background jobs that the client
# polls; jobs survive server restarts (state lives in .gwjobs/).


def _job_paths(job_id):
    return (JOBS_DIR / f"{job_id}.json", JOBS_DIR / f"{job_id}.log",
            JOBS_DIR / f"{job_id}.rc")


def _pid_is_ours(pid):
    """Alive and still a gwscript process (guards against pid reuse)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    zombie = stat.rsplit(")", 1)[-1].split()[0] == "Z"
    return not zombie and Path(sys.argv[0]).name in cmdline


def _tail(path, lines):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def job_info(job_id, tail=0):
    meta_path, log, rc_path = _job_paths(job_id)
    meta = json.loads(meta_path.read_text())

    if rc_path.exists():
        rc = int(rc_path.read_text().strip() or 1)
        meta["state"] = {0: "succeeded", 130: "interrupted"}.get(rc, "failed")
        meta["returncode"] = rc
    elif (job_id in _CHILDREN and _CHILDREN[job_id].poll() is None) or \
            (job_id not in _CHILDREN and _pid_is_ours(meta["pid"])):
        meta["state"] = "running"
    else:
        meta["state"] = "lost"          # killed without writing an exit code

    started = datetime.fromisoformat(meta["started"])
    end = datetime.fromtimestamp(rc_path.stat().st_mtime) if rc_path.exists() \
        else datetime.now()
    meta["elapsed_sec"] = round((end - started).total_seconds())

    if tail:
        meta["log_tail"] = _tail(log, tail)
    return meta


def list_jobs(limit=10):
    if not JOBS_DIR.is_dir():
        return []
    ids = sorted(p.stem for p in JOBS_DIR.glob("*.json"))[-limit:]
    return [job_info(i) for i in reversed(ids)]


def start_job(gw_args):
    import shlex

    running = [j for j in list_jobs(50) if j["state"] == "running"]
    if running:
        return {"error": f"job {running[0]['id']} is still running",
                "job": running[0]}

    JOBS_DIR.mkdir(exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    meta_path, log, rc_path = _job_paths(job_id)

    cmd = [sys.executable, "-u", str(Path(sys.argv[0]).resolve()), *gw_args]
    env = {**os.environ, "GW_NONINTERACTIVE": "1", "GW_LOG": str(log),
           "GW_JOB_RC": str(rc_path)}

    with log.open("w") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)   # outlives the server

    meta = {"id": job_id, "args": gw_args, "command": shlex.join(cmd),
            "pid": proc.pid, "started": _now(), "log": str(log)}
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    _CHILDREN[job_id] = proc           # reaped by poll() so no zombies linger
    return job_info(job_id)


_CHILDREN = {}


def _gw_cli(*args, timeout=600):
    """Run this script synchronously (read-only queries, clean)."""
    for proc in _CHILDREN.values():
        proc.poll()
    r = subprocess.run(
        [sys.executable, str(Path(sys.argv[0]).resolve()), *args],
        cwd=ROOT, text=True, capture_output=True, timeout=timeout,
        env={**os.environ, "GW_NONINTERACTIVE": "1"}, stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"exit {r.returncode}")
    return r.stdout


def _gw_json(*args):
    return json.loads(_gw_cli(*args, "--json"))


def serve_mcp():
    try:
        from mcp.server.mcpserver import MCPServer          # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
        except ImportError:
            raise RuntimeError("MCP mode needs the 'mcp' package: pip install mcp")
    from mcp.types import ToolAnnotations

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    acts = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                           openWorldHint=False)

    server = MCPServer(
        "gwscript",
        instructions=(
            f"GarageWorks build/test workflow for the project at {ROOT}. "
            "Stages: " + ", ".join(STAGE_ORDER) + ". Use `status` and "
            "`dry_run` before `run`. `run` starts a background job and returns "
            "at once; poll `job` (and `log`) for progress. FPGA stages need "
            "fpga=true. `cancel` stops a job and marks its stage interrupted."
        ),
    )

    def flags(fpga):
        return ["--fpga"] if fpga else []

    @server.tool(annotations=read_only)
    def status() -> dict:
        """Stage states (pending/running/success/failed/interrupted/stale with
        reason), live elapsed time for running stages, and recent jobs."""
        report = _gw_json("--status")
        report["jobs"] = list_jobs(5)
        return report

    @server.tool(annotations=read_only)
    def dry_run(stage: str | None = None, resume_from: str | None = None,
                fpga: bool = False) -> dict:
        """What `run` would execute and why, without running anything."""
        args = ["--dry-run", *flags(fpga)]
        if resume_from:
            args += ["--resume", resume_from]
        if stage:
            args.append(stage)
        return _gw_json(*args)

    @server.tool(annotations=read_only)
    def next_stage() -> dict:
        """The next runnable stage and the state of its dependencies."""
        return _gw_json("--next")

    @server.tool(annotations=read_only)
    def results(stage: str) -> dict:
        """Recorded result, timing and provenance (tools, git, config) of a stage."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}; one of {STAGE_ORDER}")
        return load_stage_results().get(stage) or {}

    @server.tool(annotations=read_only)
    def log(stage: str | None = None, job_id: str | None = None,
            tail: int = 200) -> str:
        """Last lines of a job log (default: latest job) or of a tmux stage log
        (fpgatiming, buildhw), which is where Vivado output goes."""
        if stage:
            if stage not in TMUX_STAGES:
                raise ValueError(f"{stage} has no own log; use job_id instead")
            return _tail(LOG_DIR / f"{stage}.log", tail)
        jobs = [job_info(job_id)] if job_id else list_jobs(1)
        if not jobs:
            return "no jobs yet"
        return _tail(jobs[0]["log"], tail)

    @server.tool(annotations=acts)
    def run(stage: str | None = None, stage_args: list[str] | None = None,
            resume_from: str | None = None, fpga: bool = False) -> dict:
        """Start a background job. No stage: resume the workflow (re-runs
        pending/failed/interrupted/stale stages). stage: run only that stage.
        resume_from: force-run from that stage on. fpga=true enables FPGA
        stages (builds take hours; programhw/testhw use the board)."""
        args = flags(fpga)
        if resume_from:
            args += ["--resume", resume_from]
        if stage:
            args += [stage, *(stage_args or [])]
        return start_job(args)

    @server.tool(annotations=read_only)
    def job(job_id: str | None = None, tail: int = 40) -> dict:
        """State (running/succeeded/failed/interrupted/lost), elapsed time and
        log tail of a job (default: latest)."""
        if not job_id:
            jobs = list_jobs(1)
            if not jobs:
                return {"error": "no jobs yet"}
            job_id = jobs[0]["id"]
        return job_info(job_id, tail)

    @server.tool(annotations=read_only)
    def jobs(limit: int = 10) -> list:
        """Recent jobs, newest first."""
        return list_jobs(limit)

    @server.tool(annotations=acts)
    def cancel(job_id: str | None = None) -> dict:
        """Stop a running job (SIGTERM). Its stage is recorded as interrupted;
        a tmux build is killed too."""
        info = job_info(job_id) if job_id else next(
            (j for j in list_jobs(50) if j["state"] == "running"), None)
        if not info or info["state"] != "running":
            return {"error": "no running job"}
        os.kill(info["pid"], signal.SIGTERM)
        return {"cancelled": info["id"]}

    @server.tool(annotations=acts)
    def clean(all: bool = False) -> str:
        """Remove state and log files; all=true also removes the AVED-gw clone."""
        return _gw_cli("--clean-all" if all else "--clean")

    @server.tool(annotations=read_only)
    def sudoers() -> str:
        """The sudoers line programhw needs for non-interactive runs."""
        return sudoers_line()

    server.run()


def main():
    """Entry point; records the exit code for MCP jobs (GW_JOB_RC)."""
    rc = 1
    try:
        _main()
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    finally:
        if os.environ.get("GW_JOB_RC"):
            Path(os.environ["GW_JOB_RC"]).write_text(f"{rc}\n")


def _main():
    global FPGA_ENABLED

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-C", "--root",
        metavar="DIR",
        help="Project directory (default: current directory)",
    )

    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Serve gwscript as an MCP server over stdio (needs: pip install mcp)",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output for --status, --next and --dry-run",
    )

    parser.add_argument(
        "--sudoers",
        action="store_true",
        help="Print the sudoers line needed for non-interactive (MCP/tmux) runs",
    )

    parser.add_argument(
        "--fpga",
        action="store_true",
        help="Enable FPGA stages (aved, buildhw, programhw, testhw)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which stages would run and why, without running them",
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove state and log files (.gwstatus, .gwstage-results.json, logs)",
    )

    parser.add_argument(
        "--clean-all",
        action="store_true",
        help="Same as --clean, and also remove the AVED-gw directory",
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Show stage status (including computed 'stale')",
    )

    parser.add_argument(
        "--accept",
        nargs="+",
        metavar="STAGE",
        choices=list(STAGES.keys()),
        help="Mark a stage's current inputs as up to date without re-running it",
    )

    parser.add_argument(
        "--next",
        action="store_true",
        help="Show the next runnable stage",
    )

    parser.add_argument(
        "--resume",
        nargs="?",
        const="__auto__",
        choices=["__auto__"] + list(STAGES.keys()),
        metavar="STAGE",
        help=(
            "Resume required workflow from STAGE, or from the first unfinished "
            "or stale required stage when STAGE is omitted"
        ),
    )

    parser.add_argument(
        "--direct-stage",
        choices=STAGES.keys(),
        help=argparse.SUPPRESS,
    )

    parser.add_argument("--env-file", help=argparse.SUPPRESS)

    parser.add_argument(
        "stage",
        nargs="?",
        default=None,
        choices=STAGES.keys(),
        help="Run exactly one stage (default: resume workflow)",
    )

    parser.add_argument(
        "stage_args",
        nargs="*",
        help="Optional arguments for the selected stage",
    )

    args = parser.parse_args()

    # tmux kill-session sends SIGHUP; kill sends SIGTERM -> recorded as interrupted
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGHUP, _on_signal)

    FPGA_ENABLED = args.fpga or (args.stage in FPGA_STAGES) \
        or (args.direct_stage in FPGA_STAGES)

    if args.sudoers:
        print(sudoers_line())
        return

    if args.mcp:
        serve_mcp()
        return

    # child process inside tmux: the parent holds the project lock
    if args.direct_stage:
        if args.env_file:
            env_path = Path(args.env_file)
            os.environ.update(json.loads(env_path.read_text()))
            env_path.unlink(missing_ok=True)       # may hold credentials
        try:
            _execute_stage(args.direct_stage)
        except (KeyboardInterrupt, StageInterrupted):
            print(f"\n{args.direct_stage}: interrupted", file=sys.stderr)
            sys.exit(130)
        except subprocess.CalledProcessError as e:
            print(
                f"\nERROR: command failed with exit code {e.returncode}: "
                f"{_cmd_str(e.cmd)}",
                file=sys.stderr,
            )
            sys.exit(e.returncode or 1)
        except Exception as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return

    lock = None
    total_start = time.monotonic()

    try:
        if args.clean or args.clean_all:
            lock = _acquire_project_lock()
            _refuse_if_busy(_recover_running())
            clean(all_=args.clean_all)
            return

        if not STATUS_FILE.exists():
            save_status(default_status())

        if args.accept:
            lock = _acquire_project_lock()
            for name in args.accept:
                accept_stage(name)
            return

        if args.status or args.next or args.dry_run:
            # read-only; tidy up dead 'running' entries only if nobody else is active
            probe = FileLock(LOCK_FILE)
            if probe.acquire():
                try:
                    _recover_running()
                finally:
                    probe.release()

            if args.status:
                show_status(args.json)
            elif args.next:
                show_next(args.json)
            else:
                if args.resume is not None and args.stage is not None:
                    raise RuntimeError(
                        "Use either '--resume [STAGE]' or a positional stage, not both"
                    )
                start = None if args.resume in (None, "__auto__") else args.resume
                dry_run(args.stage, args.stage_args, start, args.json)
            return

        lock = _acquire_project_lock()
        _refuse_if_busy(_recover_running())

        print(f"Project root: {ROOT}")

        if args.resume is not None:
            if args.stage is not None or args.stage_args:
                raise RuntimeError(
                    "Use either '--resume [STAGE]' or a positional stage, not both"
                )
            start_stage = None if args.resume == "__auto__" else args.resume
            resume_workflow(start_stage)
        else:
            if args.stage is None:
                if args.stage_args:
                    raise RuntimeError(
                        "Stage arguments require an explicit stage name"
                    )
                resume_workflow()
            else:
                run_named_stage(args.stage, args.stage_args)

    except (KeyboardInterrupt, StageInterrupted):
        print_timing_summary(time.monotonic() - total_start)
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        print_timing_summary(time.monotonic() - total_start)
        print(
            f"\nERROR: command failed with exit code {e.returncode}: "
            f"{_cmd_str(e.cmd)}",
            file=sys.stderr,
        )
        sys.exit(e.returncode or 1)
    except Exception as e:
        print_timing_summary(time.monotonic() - total_start)
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if lock:
            lock.release()

    print_timing_summary(time.monotonic() - total_start)


if __name__ == "__main__":
    main()
