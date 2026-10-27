# Development status

A running, concise record of what's implemented, what's been verified
end-to-end, recent CLI-facing changes, and known limitations. Kept up to
date as `gwscript` and the bridges evolve; see [development-notes.md](development-notes.md)
for the architecture itself.

Last updated: 2026-10-26 (after commit `1d7cb16`).

## Implemented

- **Stage pipeline**: `prereq -> rtlgen -> cocotb -> fpgatiming -> aved ->
  buildhw -> programhw -> testhw`, driven by `gwscript` with file-based state
  (`.gwstatus`, `.gwstage-results.json`) and an exclusive per-project lock
  (`.gwlock`).
- **FPGA gating**: `--fpga` (or persisted `fpga=true` via `--enable-fpga`)
  enables the hardware stages for a run; without it they're skipped. As of
  `fb92b92`, the reverse also holds: `--fpga` skips `cocotb` automatically
  during the normal pipeline (it's simulation-only), but `cocotb` can still
  be run explicitly by name (`gwscript cocotb --fpga`).
- **Pluggable FPGA backends**: `garageworks.gwscript.backends.Backend`
  (`fetch`/`build`/`program`/`test`), selected via `.gwconfig`'s `platform=`
  or `--platform`, with a built-in `aved` backend and a project-local
  `gwbackends/` extension point for custom ones.
- **Bridges**: `cocotb_bridge` (simulation) and `aved_bridge` (FPGA via
  pyaved) share one testbench; `conv_cocotb_to_fpga` translates a cocotb
  testbench to the FPGA variant source-to-source.
- **MCP server** (`gwscript --mcp`): exposes the same workflow as MCP tools
  (`status`, `dry_run`, `next_stage`, `results`, `log`, `run`, `job`, `jobs`,
  `cancel`, `clean`, `sudoers`). Long stages run as background jobs (or
  detached `tmux` sessions for `fpgatiming`/`buildhw`) that survive the
  client disconnecting; state is shared with the plain CLI (same lock, same
  status files).
- **Non-interactive setup**: `gwscript --setup <package>.<ClassName>`
  scaffolds `.gwconfig`/`Makefile.gw`/`target.mk` without the interactive
  wizard.
- **Packaging**: Chisel library published to Sonatype Central Maven
  snapshots (`io.github.hwspec::garageworks`); Python package (`gwscript` +
  bridges) published to PyPI as dev (`.devN`) builds only, no stable release
  yet.
- **MkDocs site** (`mkdocs.yml`, added this session): renders `docs/` with
  the `readthedocs` theme; `mkdocs build --strict` passes clean.

## Verified examples

- **SRV32I, cocotb simulation, plain CLI**: `gwscript` (no flags) runs
  `prereq -> rtlgen -> cocotb` and passes (`TESTS=1 PASS=1 FAIL=0`).
- **SRV32I, cocotb simulation, via MCP** (2026-10-26): installed `mcp` in
  the `testgw` conda env, drove `gwscript --mcp` with a standalone MCP
  client over stdio. Verified: tool listing (all 11 tools), `status` /
  `dry_run` / `next_stage` / `sudoers` (read-only), `run` with no args
  correctly resuming only the sim path and leaving FPGA-only stages
  `pending`, `job` polling through `running -> succeeded` with live log
  tails, and a passing cocotb result. No leftover processes/lock after the
  run. **Not exercised in this pass**: `cancel`, `clean`, `results`.
- **FPGA hardware stages** (`aved`/`buildhw`/`programhw`/`testhw`): exist
  and were previously run against a real AMD Alveo V80 + AVED board on a
  separate project (`gw-testq`) before the backend-abstraction work landed.
  Not yet re-verified on this machine (no FPGA attached here) or via MCP —
  that needs the node with the board attached, `fpga=true`, and
  `--platform aved` (or whatever's current there).

## Recent CLI changes

- `--fpga` / `--enable-fpga` / `--disable-fpga` / `--platform NAME` — FPGA
  stage gating and backend selection (`84b6c58`).
- `--fpga` now implicitly skips `cocotb` in the automatic pipeline unless
  the stage is named explicitly (`fb92b92`).
- `--mcp`, `--json`, `--sudoers`, `--accept`, `--next`, `--resume [STAGE]`,
  `--dry-run`, `--clean` / `--clean-all`, `-C/--root DIR`, `--setup
  CHISELTOP` — see `gwscript --help` for the full current surface.
- Top-level repo `Makefile` removed; `python/Makefile` (used by the
  currently-disabled CI job) still exists and is unaffected.

## Known limitations

- No stable PyPI release yet — only `.devN` builds; install unpinned
  (`pip install garageworks`).
- FPGA backend support is effectively AVED/V80-only; the `Backend`
  interface and `gwbackends/` extension point exist, but no second backend
  has been implemented yet.
- MCP server supports exactly one background job at a time per project
  (`run` refuses to start a second while one is `running`).
- CI (`.github/workflows/test.yml`) is disabled (`if: ${{ false }}`); no
  automated test suite currently runs `gwscript`/MCP end-to-end — the
  verification above was manual.
- Editable installs (`pip install -e .`) can go stale across package
  renames/moves (PEP 660 `.pth`/finder files cache paths); no automated
  check for this yet.
- MkDocs site isn't published anywhere (no `gh-deploy`/CI wiring); local
  `mkdocs serve`/`mkdocs build` only.
