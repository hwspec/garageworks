import os
import signal

from .config import LOG_DIR, ROOT, STAGE_ORDER, TMUX_STAGES
from .jobs import _gw_cli, _gw_json, _tail, job_info, list_jobs, start_job
from .status import get_stage_info
from .sudoers import sudoers_line


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
        """Recorded result, timing and provenance (tools, git, config) of a stage.
        For a platform-scoped stage (aved/buildhw/programhw/testhw), this is
        the active platform's result (see --platform)."""
        if stage not in STAGE_ORDER:
            raise ValueError(f"unknown stage {stage}; one of {STAGE_ORDER}")
        return get_stage_info(stage) or {}

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
        """Remove state and log files; all=true also removes every configured
        backend's clone dir (e.g. AVED-gw)."""
        return _gw_cli("--clean-all" if all else "--clean")

    @server.tool(annotations=read_only)
    def sudoers() -> str:
        """The sudoers line programhw needs for non-interactive runs."""
        return sudoers_line()

    server.run()
