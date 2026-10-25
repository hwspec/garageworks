import os
import re
import shutil
import socket
import subprocess
import sys
import time

from .config import (
    AMI_TOOL,
    AVED,
    AVED_HW,
    AVED_REPO,
    BDF,
    BOARD_LOCK_FILE,
    BOARD_STAGES,
    GENERATED,
    GW_RTL,
    LOG_DIR,
    NAME,
    PACKAGE,
    ROOT,
    STAGE_DEPS,
    SUDO_STAGES,
    TB_DIR,
    TESTS,
    StageInterrupted,
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
    save_stage_results,
    set_stage_status,
)
from .sudoers import _check_nopasswd_sudo
from .timing import fpgatiming


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
    prog_result = run(["make", "prog"], cwd=AVED)

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
    env = os.environ.copy()
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
            ["make", "-f", "Makefile.gw", f"hw_{target}"],
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
