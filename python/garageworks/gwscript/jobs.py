import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import JOBS_DIR, LOG_DIR, LOG_KEEP, ROOT, StageInterrupted, _now, stage_results, stage_times
from .shell import _tmux_alive, _tmux_session, run
from .stages import _latest_attempt_succeeded
from .status import (
    get_stage_info,
    get_stage_result,
    load_status,
    record_stage_failure,
    set_stage_status,
)


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
