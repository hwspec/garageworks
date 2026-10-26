import os
import re
import socket
import time

from . import backends, config
from .config import (
    BOARD_STAGES,
    GENERATED,
    LOG_DIR,
    NAME,
    PACKAGE,
    ROOT,
    STAGE_DEPS,
    SUDO_STAGES,
    TB_DIR,
    TESTS,
    StageInterrupted,
    board_lock_file,
    _noninteractive,
    _now,
    stage_results,
    stage_times,
)
from .lock import FileLock
from .shell import capture, run
from .status import (
    _fingerprint,
    get_stage_info,
    load_stage_results,
    provenance,
    record_stage_failure,
    record_stage_success,
    result_key,
    save_stage_results,
    set_stage_status,
)
from .sudoers import _check_nopasswd_sudo
from .timing import fpgatiming


def _backend():
    return backends.resolve_backend(config.PLATFORM, config.CONFIG)


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

    run(["make", "-f", "Makefile.gw", "-C", str(TB_DIR), "clean"])

    for testname in TESTS:
        print(f"\nRunning cocotb test target: {testname}")
        run(["make", "-f", "Makefile.gw", "-C", str(TB_DIR), testname])

    return {
        "testdir": str(TB_DIR),
        "tests": TESTS,
    }


def aved():
    return _backend().fetch()


def buildhw():
    return _backend().build()


def programhw():
    return _backend().program()


def testhw(testname=None):
    return _backend().test(testname)


STAGES = {
    "prereq":     (STAGE_DEPS["prereq"], prereq),
    "rtlgen":     (STAGE_DEPS["rtlgen"], rtlgen),
    "cocotb":     (STAGE_DEPS["cocotb"], cocotb),
    "fpgatiming": (STAGE_DEPS["fpgatiming"], fpgatiming),
    "aved":       (STAGE_DEPS["aved"], aved),
    "buildhw":    (STAGE_DEPS["buildhw"], buildhw),
    "programhw":  (STAGE_DEPS["programhw"], programhw),
    "testhw":     (STAGE_DEPS["testhw"], testhw),
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
        backend = _backend()
        board_lock = FileLock(board_lock_file(backend.lock_key), world_writable=True)
        if not board_lock.acquire():
            raise RuntimeError(
                f"board {backend.lock_key} is in use ({board_lock.holder()})"
            )

    fingerprint = _fingerprint(name)
    prov = provenance(name)

    data = load_stage_results()
    log = os.environ.get("GW_LOG")
    if os.environ.get("GW_IN_TMUX"):
        log = str(LOG_DIR / f"{name}.log")
    data.setdefault(result_key(name), {})["running"] = {
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
