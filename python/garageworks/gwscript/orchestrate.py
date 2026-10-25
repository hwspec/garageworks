import json
import shutil

from . import config
from .config import (
    AVED,
    BOARD_STAGES,
    CLEAN_DIRS,
    CLEAN_FILES,
    FPGA_SKIP_REASON,
    FPGA_STAGES,
    OPTIONAL_STAGES,
    REQUIRED_STAGE_ORDER,
    STAGE_DEPS,
    STAGE_ORDER,
    SUDO_STAGES,
    TMUX_STAGES,
    stage_times,
)
from .jobs import _check_stage_args, _run_stage_in_tmux
from .stages import _execute_stage
from .status import load_stage_results, load_status, stage_state


def run_named_stage(name, stage_args=None):
    """Run exactly one named stage; do not run dependencies."""
    stage_args = stage_args or []

    if name in FPGA_STAGES and not config.FPGA_ENABLED:
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
        if not config.FPGA_ENABLED:
            return [("fpgatiming", "skip", FPGA_SKIP_REASON)]
        return [("fpgatiming", "run", "requested")]

    order = REQUIRED_STAGE_ORDER
    forced = start_stage is not None

    if forced and start_stage not in order:
        raise RuntimeError(f"Unknown required resume stage: {start_stage}")

    names = order[order.index(start_stage):] if forced else order
    status, data, memo = load_status(), load_stage_results(), {}
    plan, will_run = [], set()

    for name in names:
        if name in FPGA_STAGES and not config.FPGA_ENABLED:
            plan.append((name, "skip", FPGA_SKIP_REASON))
            continue

        state, reason = stage_state(name, status, data, memo)

        if forced:
            why = "forced by --resume"
        elif state == "success":
            continue
        else:
            deps = STAGE_DEPS[name]
            missing = [
                dep for dep in deps
                if dep not in OPTIONAL_STAGES
                and dep not in will_run
                and stage_state(dep, status, data, memo)[0] != "success"
                and not (dep in FPGA_STAGES and not config.FPGA_ENABLED)
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
        deps = STAGE_DEPS[stage]
        not_ok = [d for d in deps
                  if stage_state(d, status, data, memo)[0] != "success"]
        plan = [(stage, "run", "requested" + (
            f"; WARNING deps not success: {', '.join(not_ok)}" if not_ok else ""
        ))]
        if stage in FPGA_STAGES and not config.FPGA_ENABLED:
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
