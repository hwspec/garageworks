#!/usr/bin/env python3
#
# Copyright (c) 2026, UChicago Argonne, LLC.
# License: See LICENSE in the project top-level directory.
# Main author: Kazutomo Yoshii <kazutomo@anl.gov>
#
# gwscript: GarageWorks build/test driver (RTL gen -> cocotb sim -> V80 FPGA)
#
# Dependencies
#   Python  : 3.8+ on Linux, standard library only (pip install garageworks for
#             the cocotb/AVED bridges)
#
#   Simulation stages (prereq, rtlgen, cocotb):
#     make       cocotb test targets
#     Verilator  5.044 recommended (cocotb simulator)
#     GCC        11.1.0 recommended (Verilator C++ build)
#     Java + sbt Chisel RTL generation (rtlgen)
#     cocotb     pip-installed alongside garageworks, not a build stage
#
#   FPGA stages (--fpga: fpgatiming, aved, buildhw, programhw, testhw), for
#   the default --platform=aved:
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

import os
import re
import signal
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _noninteractive():
    """True under MCP, inside tmux, or without a terminal to prompt on."""
    return bool(os.environ.get("GW_IN_TMUX") or os.environ.get("GW_NONINTERACTIVE")
                or not sys.stdin.isatty())


def _early_chdir(argv):
    """Honor -C/--root before the module reads .gwconfig from the cwd."""
    for i, arg in enumerate(argv):
        if arg in ("-C", "--root") and i + 1 < len(argv):
            os.chdir(argv[i + 1])
            return
        if arg.startswith("--root="):
            os.chdir(arg.split("=", 1)[1])
            return
        if arg.startswith("-C") and arg != "-C":
            os.chdir(arg[2:])
            return


_early_chdir(sys.argv[1:])

ROOT = Path.cwd().resolve()
CONFIG_FILE = ROOT / ".gwconfig"


_SKIP_WIZARD_FLAGS = {"-h", "--help", "--version"}
# --clean/--clean-all must work even without a project (e.g. right after a
# `git clean`, or a directory that was never set up) -- they only remove
# state/log files, nothing they touch requires chiseltop.
_CLEAN_FLAGS = {"--clean", "--clean-all"}


def _early_setup_arg(argv):
    """Find --setup CHISELTOP (or --setup=CHISELTOP) before argparse runs."""
    for i, arg in enumerate(argv):
        if arg == "--setup" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--setup="):
            return arg.split("=", 1)[1]
    return None


def _early_gwcd_arg(argv):
    """Find --gwcd TARGET (or --gwcd=TARGET) before argparse runs."""
    for i, arg in enumerate(argv):
        if arg == "--gwcd" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--gwcd="):
            return arg.split("=", 1)[1]
    return None


# True when this run just scaffolded .gwconfig (via --setup or the
# interactive wizard) because it didn't exist yet -- set inside load_config().
# main() checks this to stop after setup instead of falling through to the
# stage-running workflow.
JUST_SETUP = False


def load_config():
    global JUST_SETUP
    config = {}
    argv = sys.argv[1:]
    setup_arg = _early_setup_arg(argv)
    gwcd_arg = _early_gwcd_arg(argv)
    clean_only = bool(_CLEAN_FLAGS & set(argv))

    if not CONFIG_FILE.exists():
        if setup_arg is not None:
            from . import setup_wizard   # local import: avoid a config<->setup_wizard cycle
            config = setup_wizard.run_automated_setup(ROOT, CONFIG_FILE, setup_arg)
            JUST_SETUP = True
        elif gwcd_arg is not None:
            pass   # --gwcd owns its own validation/scaffolding; nothing to load yet
        elif clean_only:
            pass   # nothing to load; clean() only needs ROOT-derived paths
        elif not _noninteractive() and not (_SKIP_WIZARD_FLAGS & set(argv)):
            from . import setup_wizard   # local import: avoid a config<->setup_wizard cycle
            config = setup_wizard.run_setup_wizard(ROOT, CONFIG_FILE)
            JUST_SETUP = True
        else:
            raise RuntimeError(
                f"Missing config file: {CONFIG_FILE}\n"
                f"gwscript must be run from a project directory containing .gwconfig,\n"
                f"or pointed at one with -C/--root DIR. Current directory: {ROOT}\n"
                "\n"
                "Create .gwconfig in the project root with:\n"
                "  chiseltop=<package>.<top-level Chisel module>\n"
                "  testdir=<testbench directory> (optional, default: current directory)\n"
                "\n"
                "Or run: gwscript --setup <package>.<ClassName>\n"
                "Or, if no GWCD exists yet: gwscript --gwcd filename[:topmodule]"
            )
    else:
        if setup_arg is not None:
            raise RuntimeError(
                f"{CONFIG_FILE} already exists; remove it first to re-run --setup."
            )
        for raw in CONFIG_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

    if clean_only or gwcd_arg is not None:
        return config

    required = ["chiseltop"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RuntimeError(
            "Missing required .gwconfig keys: " + ", ".join(missing)
        )

    if "." not in config["chiseltop"]:
        raise RuntimeError(
            "chiseltop must be '<package>.<ClassName>', "
            f"e.g. chiseltop=srv32i.AxiSRV32I (got: {config['chiseltop']})"
        )

    return config


try:
    CONFIG = load_config()
except RuntimeError as e:
    print(f"gwscript: {e}", file=sys.stderr)
    sys.exit(1)
CHISELTOP = CONFIG.get("chiseltop", "")
PACKAGE, _, NAME = CHISELTOP.rpartition(".")
TESTS = [
    item.strip()
    for item in CONFIG.get("tests", "").split(",")
    if item.strip()
]

# testbench directory (Makefile with <test> and hw_<test> targets)
TB_DIR_NAME = CONFIG.get("testdir", ".")
TB_DIR = ROOT / TB_DIR_NAME

GENERATED = ROOT / "generated" / NAME

# FPGA backend (see gwscript/backends/): which board/toolchain the FPGA
# stages target. Overridable with --platform; backend-specific settings are
# namespaced '<platform>.key=' in .gwconfig (e.g. aved.repo=, aved.bdf=).
PLATFORM = CONFIG.get("platform", "aved")
# user-extensible: project-local backend modules live here (module:ClassName)
BACKEND_DIR = ROOT / "gwbackends"

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

# FPGA stages with independent status/results per platform (see
# configured_platforms() below); fpgatiming stays platform-agnostic since it
# never touches a backend's clone dir, only generated/ and Vivado timing.
PLATFORM_STAGES = {"aved", "buildhw", "programhw", "testhw"}


if "fpgatest" in CONFIG:
    print("WARNING: 'fpgatest' in .gwconfig is ignored; use --fpga instead.",
          file=sys.stderr)

# Set in main(): fpga=true in .gwconfig (see --enable-fpga/--disable-fpga),
# or --fpga to enable for just this run.
FPGA_ENABLED = False
FPGA_SKIP_REASON = "FPGA stages disabled (use --fpga, or --enable-fpga to turn on by default)"

# Simulation-only stages skipped when running against real FPGA hardware.
SIM_ONLY_STAGES = {"cocotb"}
SIM_ONLY_SKIP_REASON = "cocotb skipped (--fpga specified)"


def fpga_enabled_default():
    return CONFIG.get("fpga", "").strip().lower() == "true"


def set_config_value(key, value):
    """Persist key=value into .gwconfig, replacing an existing line for key
    (preserving comments/other lines) or appending a new one."""
    lines = CONFIG_FILE.read_text().splitlines() if CONFIG_FILE.exists() else []
    new_lines, found = [], False

    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(raw)

    if not found:
        new_lines.append(f"{key}={value}")

    CONFIG_FILE.write_text("\n".join(new_lines) + "\n")
    CONFIG[key] = value

CLEAN_FILES = [
    STATUS_FILE,
    RESULTS_FILE,
    ROOT / "aved-compile.log",
    TB_DIR / "output.log",
]
CLEAN_DIRS = [LOG_DIR, ROOT / ".gwjobs"]

STAGE_ORDER = [
    "prereq",
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

# --clean-all only: files that require re-running --setup/the wizard to
# replace, so plain --clean leaves them alone.
CLEAN_ALL_FILES = [CONFIG_FILE, LOCK_FILE]


def board_lock_file(key):
    """Lock file path serializing BOARD_STAGES access to one backend's board
    (key is the backend's Backend.lock_key, e.g. its BDF)."""
    return Path(tempfile.gettempdir()) / (
        "gw-board-" + re.sub(r"[^0-9A-Za-z]", "_", str(key)) + ".lock"
    )


def configured_platforms():
    """Every platform with state worth showing/cleaning: the active one plus
    any '<platform>.key=' namespace found in .gwconfig."""
    platforms = {PLATFORM}
    platforms.update(key.split(".", 1)[0] for key in CONFIG if "." in key)
    return sorted(platforms)


# archived logs kept per tmux stage in .gwlogs (current log + LOG_KEEP old)
LOG_KEEP = int(CONFIG.get("log_keep", "5"))

# Inputs that make a successful stage stale when they change (besides an
# upstream stage re-running).  globs are relative to the project root.
STAGE_INPUTS = {
    "rtlgen": {
        "globs": ["src/**/*.scala", "build.sbt", "project/*.sbt",
                  "project/build.properties"],
        "config": ["chiseltop"],
    },
    "cocotb": {
        "globs": [f"{TB_DIR_NAME}/{g}" for g in
                  ("*.py", "Makefile", "*.s", "*.c", "*.tbspec")],
        "exclude": [f"{TB_DIR_NAME}/fpga_*.py"],   # generated by make hw_<test>
        "config": ["tests"],
    },
    # Platform-scoped stages (aved/buildhw/programhw) don't list backend
    # config/git here: the active backend's clone-dir git commit and
    # '<platform>.*' config keys are hashed automatically (see
    # status._inputs_hash) since those vary per backend.
    "testhw": {
        "globs": [f"{TB_DIR_NAME}/*.py", f"{TB_DIR_NAME}/Makefile"],
        "exclude": [f"{TB_DIR_NAME}/fpga_*.py"],
        "config": ["tests"],
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

# Stage dependency graph (pure data, no function references).  Kept separate
# from stages.STAGES (which pairs each name with its implementation function)
# so status.py can consult dependencies without importing stages.py, which
# itself depends on status.py -- avoiding a circular import.
STAGE_DEPS = {
    "prereq": [],
    "rtlgen": [],
    "cocotb": ["rtlgen"],
    "fpgatiming": ["rtlgen"],
    "aved": ["prereq"],
    "buildhw": ["rtlgen", "aved"],
    "programhw": ["buildhw"],
    "testhw": ["programhw"],
}


class StageInterrupted(BaseException):
    """Raised by the SIGTERM/SIGHUP handler (tmux kill-session sends SIGHUP)."""


def _on_signal(signum, frame):
    raise StageInterrupted(signal.Signals(signum).name)
