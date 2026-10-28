import argparse
import json
import os
import signal
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config
from .config import LOCK_FILE, STAGE_ORDER, STATUS_FILE, StageInterrupted
from .lock import FileLock, _acquire_project_lock, _recover_running, _refuse_if_busy
from .mcp_server import serve_mcp
from .orchestrate import clean, dry_run, print_timing_summary, resume_workflow, run_named_stage
from .shell import _cmd_str
from .stages import _execute_stage
from .status import accept_stage, default_status, load_status, save_status, show_next, show_status
from .sudoers import sudoers_line


def _version():
    try:
        return version("garageworks")
    except PackageNotFoundError:
        return "unknown"


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
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--version",
        action="version",
        version=f"gwscript {_version()}",
    )

    parser.add_argument(
        "-C", "--root",
        metavar="DIR",
        help="Project directory (default: current directory)",
    )

    parser.add_argument(
        "--setup",
        metavar="CHISELTOP",
        help=(
            "Non-interactively create .gwconfig/Makefile.gw/<pkg>.mk for "
            "CHISELTOP (package.ClassName), using gwtests as the test "
            "directory, then exit. Fails if .gwconfig already exists."
        ),
    )

    parser.add_argument(
        "--gwcd",
        metavar="TARGET[:TOP]",
        help=(
            "Generate a GarageWorks Command Dispatcher (AXI4-Lite wrapper + "
            "ScalaTest spec) around TARGET (a plain Chisel .scala source, or "
            "a simple ANSI-style Verilog .v/.sv module), compile/test it, "
            "then exit. TOP names the DUT class/module when it doesn't match "
            "the filename. Requires ANTHROPIC_API_KEY or OPENAI_API_KEY."
        ),
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
        help=(
            "Enable FPGA stages (aved, buildhw, programhw, testhw) for this "
            "run only; see --enable-fpga to make this the default"
        ),
    )

    fpga_default_group = parser.add_mutually_exclusive_group()
    fpga_default_group.add_argument(
        "--enable-fpga",
        action="store_true",
        help="Persist fpga=true in .gwconfig (FPGA stages run by default), then exit",
    )
    fpga_default_group.add_argument(
        "--disable-fpga",
        action="store_true",
        help="Persist fpga=false in .gwconfig (FPGA stages off by default), then exit",
    )

    parser.add_argument(
        "--platform",
        metavar="NAME",
        help=(
            "FPGA backend to target (default: aved, or 'platform=' in "
            ".gwconfig). NAME is a built-in backend or 'module:ClassName' "
            "for a custom one in ./gwbackends/"
        ),
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
        help="Same as --clean, and also remove every configured backend's "
             "clone dir (e.g. AVED-gw), .gwconfig, and .gwlock",
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
        choices=STAGE_ORDER,
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
        choices=["__auto__"] + STAGE_ORDER,
        metavar="STAGE",
        help=(
            "Resume required workflow from STAGE, or from the first unfinished "
            "or stale required stage when STAGE is omitted"
        ),
    )

    parser.add_argument(
        "--direct-stage",
        choices=STAGE_ORDER,
        help=argparse.SUPPRESS,
    )

    parser.add_argument("--env-file", help=argparse.SUPPRESS)

    parser.add_argument(
        "stage",
        nargs="?",
        default=None,
        choices=STAGE_ORDER,
        help="Run exactly one stage (default: resume workflow)",
    )

    parser.add_argument(
        "stage_args",
        nargs="*",
        help="Optional arguments for the selected stage",
    )

    args = parser.parse_args()

    # config.py already did the scaffolding (or raised) while loading .gwconfig,
    # before argparse ever ran -- whether that was explicit (--setup) or the
    # interactive first-run wizard, stop here instead of starting stages.
    if config.JUST_SETUP:
        if not args.setup:
            print("Setup complete. Re-run gwscript to start the stages.")
        return

    # tmux kill-session sends SIGHUP; kill sends SIGTERM -> recorded as interrupted
    signal.signal(signal.SIGTERM, config._on_signal)
    signal.signal(signal.SIGHUP, config._on_signal)

    config.FPGA_ENABLED = args.fpga or config.fpga_enabled_default()

    if args.platform:
        config.PLATFORM = args.platform

    if args.enable_fpga or args.disable_fpga:
        value = "true" if args.enable_fpga else "false"
        config.set_config_value("fpga", value)
        print(f"fpga={value} ({config.CONFIG_FILE})")
        return

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
        if args.gwcd:
            if args.setup or args.stage or args.resume is not None or args.accept:
                raise RuntimeError(
                    "--gwcd cannot be combined with --setup, a stage, --resume, or --accept"
                )
            lock = _acquire_project_lock()
            _refuse_if_busy(_recover_running())
            from .gwcd import run_gwcd
            run_gwcd(args.gwcd)
            return

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

        print(f"Project root: {config.ROOT}")

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
                run_named_stage(args.stage, args.stage_args, explicit=True)

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
