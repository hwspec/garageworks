import os
import re
import subprocess
import sys

from .config import GENERATED, LOG_DIR


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


def _find_timing_report(workdir, newer_than=None):
    candidates = list(workdir.rglob("*timing*.rpt"))
    if newer_than is not None:
        candidates = [p for p in candidates if p.stat().st_mtime > newer_than]
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

    baseline = max(
        (p.stat().st_mtime for p in workdir.rglob("*timing*.rpt")), default=None
    )

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

        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                logfile.write(line)
                output_lines.append(line)
        except BaseException:
            proc.terminate()            # don't leave Vivado running
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise

        rc = proc.wait()

    output = "".join(output_lines)

    report = _find_timing_report(workdir, newer_than=baseline)
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
