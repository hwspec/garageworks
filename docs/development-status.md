# Development status

A running, concise record of what's implemented, what's been verified
end-to-end, recent CLI-facing changes, and known limitations. Kept up to
date as `gwscript` and the bridges evolve; see [development-notes.md](development-notes.md)
for the architecture itself.

Last updated: 2026-10-28 (uncommitted: `gwscript --gwcd`, tutorial rewrite,
`getGitHash` non-git fallback fix, `--gwcd` auto-adds missing garageworks dep).

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
  scaffolds `.gwconfig`/`Makefile.gw`/`<package>.mk` without the interactive
  wizard.
- **Namespaced setup scaffolding** (2026-10-28): the generated per-project
  `.mk` file and cocotb bridge are now named after chiseltop's package
  (e.g. `chiseltop=fifo.FifoGWCD` → `fifo.mk`, `fifo_bridge.py`,
  `FifoBridge`) instead of the generic `target.mk`/`target_bridge.py`,
  matching the hand-written convention already used in `gwproj-example`
  (`gcd.mk`/`gcd_bridge.py`/`GcdBridge`). `fifo.mk` reads `HWMODULENAME` and
  `PKGNAME` back out of `.gwconfig`'s `chiseltop=` at make time (via `awk`
  + `$(basename)`/`$(suffix)`) rather than baking them in, so editing
  `chiseltop` doesn't require regenerating the file.
- **Setup-only first run**: when `.gwconfig` doesn't exist yet, `gwscript`
  only scaffolds it (via `--setup` or the interactive wizard) and exits —
  it no longer falls through into running stages against a brand-new
  project. Run `gwscript` again to start the pipeline.
- **Packaging**: Chisel library published to Sonatype Central Maven
  snapshots (`io.github.hwspec::garageworks`); Python package (`gwscript` +
  bridges) published to PyPI as dev (`.devN`) builds only, no stable release
  yet.
- **MkDocs site** (`mkdocs.yml`, added this session): renders `docs/` with
  the `readthedocs` theme; `mkdocs build --strict` passes clean.
- **`docs/tutorial-chisel-dut.md`** (replaces the old `docs/tutorials.md`,
  removed): end-to-end walkthrough from a plain Chisel DUT (a
  `chisel3.util.Queue`-backed FIFO, 32-bit words, parameterized depth) and
  its ChiselSim spec, through adding GarageWorks, `--gwcd`, `sbt test`, and
  `gwscript` simulation/FPGA stages. Ships a downloadable, verified-working
  starting project at `docs/assets/fifo-example.zip` (`build.sbt` +
  `project/build.properties` + `Fifo.scala` + `FifoSpec.scala`).
- **`gwscript --gwcd TARGET[:TOP]`**: generates a GarageWorks Command
  Dispatcher (AXI4-Lite wrapper Chisel module + ScalaTest spec) around a
  plain Chisel DUT or a "simple" ANSI-style Verilog module, using an LLM
  (Anthropic primary via `output_config` structured outputs, OpenAI
  fallback via `response_format` strict JSON schema — needs
  `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, `pip install garageworks[gwcd]`).
  Bootstraps `build.sbt`/`project/` if none exists, compiles and tests the
  generated files with up to 3 LLM repair attempts, then hands off to the
  existing `.gwconfig` scaffolding. When no GWCD candidate is found, the
  setup wizard now points users at this flag instead of prompting for a
  name that doesn't exist yet. `verilog_parse.py` parses params/ports of
  simple Verilog module headers standalone. `--updategwcd` and
  non-"simple" Verilog are explicitly out of scope.

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
- **`gwscript --gwcd`, negative paths** (2026-10-27): no API key fails
  immediately before touching the filesystem; an ambiguous Chisel target
  (two DUT-shaped classes, no `:TOP`) errors listing both candidates before
  any filesystem mutation; `:TOP` disambiguation selects the right class; a
  project with no GWCD candidate and no `.gwconfig` shows the new "No
  GarageWorks Command Dispatcher found!" message (verified both as a direct
  unit call and under a real pty for the interactive wizard path). Also
  caught and fixed a real bug this surfaced: sbt's default
  `unmanagedSources` also globs loose `*.scala` files sitting directly in
  the project root (not just `src/main/scala/**`), so a `--gwcd` target
  copied straight into a fresh project directory (the obvious way to use
  the flag) collided with its own copy under `src/main/scala/<pkg>/` and
  broke the very first compile — `gwcdskills/build_sbt.tmpl` now excludes
  root-level files from `Compile / unmanagedSources`. **Not yet verified**:
  the full pipeline with a real LLM call (Anthropic or OpenAI) — this
  environment's `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` route through an
  institutional gateway rather than the real Anthropic API, which breaks
  the Python SDK's streaming assumptions on bad/test input; a real direct
  key is needed to verify the generate/compile/test/repair loop end to end
  (e.g. against `gwproj-example`'s `DecoupledGCD.scala`, or
  `verilog/Cmd/Dut.v` for the Verilog path). **Update (2026-10-28, user's own
  manual run)**: `--gwcd` against the tutorial's `Fifo.scala` generated
  `FifoGWCD.scala`/`FifoGWCDSpec.scala` and got as far as `gws`'s `rtlgen`
  stage, which then crashed running the generated module's `object
  FifoGWCD extends App` (`AxiModuleParamsHelper.getGitHash` shelling out to
  `git rev-parse HEAD` with no non-git fallback — fatal outside a git repo,
  which the tutorial's `fifo-example` is). Fixed in
  `src/main/scala/garageworks/Axi4Lite.scala`'s `getGitHash`: now falls back
  to `0` with a one-line stderr warning instead of throwing, verified both
  via `sbt compile`/`publishLocal` and a standalone non-git `/tmp` project
  calling it directly. **Resolved**: after publishing the fix as a new
  `0.4.0-SNAPSHOT` (`./scripts/publish-snapshot.sh`) to Sonatype Central, the
  user re-ran `--gwcd` + `gws` against it and it passed — this is the first
  confirmed live-LLM, end-to-end `--gwcd` run (generate, compile, test,
  `rtlgen`, cocotb) for a Chisel target. **Update (2026-10-28, another user
  run)**: against a `build.sbt` that existed but hadn't yet had the
  `garageworks` dependency added (skipping the tutorial's now-optional step
  2), `--gwcd`'s generated `FifoGWCD.scala` ended up with `import
  garageworks._` failing to resolve, triggered a `sbt compile` failure on
  attempt 1, and the repair loop's attempt 2 "fixed" that by vendoring
  `garageworks`' own symbols into the generated file under `package
  garageworks { ... }` — with `FifoGWCD` itself left outside that block, in
  no package at all (`setup_wizard`'s candidate scan, which trusts whatever
  `package` line is declared regardless of file location, then surfaced
  `garageworks.FifoGWCD` as a chiseltop candidate). Both compiled/ran fine,
  since Scala permits both an unpackaged top-level object and
  user-extending an existing package — this failure mode is invisible to
  the `sbt compile`/`sbt test` gate. Fixed in `gwcd.py`'s
  `_bootstrap_sbt_project`/`_ensure_garageworks_dep`: an existing
  `build.sbt` now gets the resolver + `garageworks` dependency appended
  automatically if missing (checked via a simple `"garageworks" in text`
  substring test), before the first `sbt compile` ever runs — left
  untouched if already present. Verified by appending to a scratch
  tutorial-style `build.sbt` and confirming `import
  garageworks.AxiModuleParamsHelper._` resolves and compiles.
- **`docs/tutorial-chisel-dut.md`'s FIFO example** (2026-10-28): the
  `Fifo.scala`/`FifoSpec.scala`/`build.sbt` shown in the doc, and the zip
  built from the identical content, were both verified with `sbt test`
  (fresh extract of the zip included) — write/read, empty/full, and
  read-after-soft-reset-is-zero all pass. The `--gwcd` step on this example
  is now verified live end-to-end too (see the `getGitHash` entry above).
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
- A first run against a project with no `.gwconfig` now stops right after
  scaffolding (interactive wizard or `--setup`) instead of continuing into
  the stage pipeline.
- `--gwcd TARGET[:TOP]` — generate a GWCD wrapper + spec around a plain
  Chisel/Verilog DUT via LLM, compile/test it, then scaffold `.gwconfig`.
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
- `--gwcd`'s live LLM path is now verified end-to-end for a Chisel target
  (see the verified-examples note above); the Verilog path (`verilog_parse.py`
  + `extmodule_direct` reset style) is still unverified with a real LLM call.
  `--updategwcd` (adding test components to an existing GWCD) and
  non-"simple" Verilog (interfaces, structs, generate blocks, non-ANSI port
  declarations) are not implemented.
