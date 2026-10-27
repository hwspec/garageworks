# Development notes

Internals and background for anyone working on GarageWorks itself (as
opposed to using it to test a design). See [tutorials.md](tutorials.md) for
the user-facing side.

## Internal structure

### Bridges

A cocotb testbench never pokes DUT signals directly; it only talks to a
bridge, which exposes common primitives (soft reset, AXI read/write,
expected-value checks) over whatever is actually under it:

- `garageworks.bridge.cocotb_bridge`: drives the DUT in a Verilator/cocotb
  simulation, through `cocotbext-axi`. Keeps cocotbext-axi's per-transaction
  AXI logging quiet by default; set `GW_AXI_LOG=1` to see it.
- `garageworks.bridge.aved_bridge`: drives the same test over an AXI4-Lite
  endpoint on real FPGA hardware, through `pyaved`.
- `garageworks.bridge.conv_cocotb_to_fpga`: a source-to-source translator
  that rewrites a simulation testbench (`tb_*.py`) into its FPGA-targeted
  form (`fpga_tb_*.py`), so the same testbench source drives both the `make
  <test>` (simulation) and `make hw_<test>` (FPGA) targets without manual
  changes.

### gwscript stage pipeline

`gwscript` drives a fixed stage pipeline, each stage recorded with its
dependencies, inputs, and provenance (host, tool versions, git commit) so
`--status`/`--resume` can tell what's stale and only re-run what's needed:

```
prereq -> rtlgen -> cocotb -> fpgatiming -> aved -> buildhw -> programhw -> testhw
```

- `prereq`/`rtlgen`/`cocotb` are the simulation stages: generate RTL from
  Chisel, then run the cocotb testbench against it on Verilator.
- `fpgatiming`/`aved`/`buildhw`/`programhw`/`testhw` are the FPGA stages,
  off by default (`--fpga` to opt in for one run, `--enable-fpga` to persist
  it in `.gwconfig`). `aved`/`buildhw`/`programhw`/`testhw` are also tracked
  per-backend/platform.
- State lives in two project-local files: `.gwstatus` (per-stage
  pending/running/success/failed/interrupted; intentionally hand-editable)
  and `.gwstage-results.json` (provenance, input fingerprints).
- `.gwlock` serializes one `gwscript` per project; `programhw`/`testhw` also
  take a per-board lock under `/tmp`, shared across users/projects.

See `python/garageworks/gwscript/config.py`'s module docstring for the exact
tool versions and dependencies each stage needs.

### Backend abstraction

FPGA stages are implemented against `garageworks.gwscript.backends.Backend`
(`fetch`/`build`/`program`/`test`), selected via `.gwconfig`'s `platform=`
key or `--platform`. The built-in backend is `aved` (AMD Alveo V80 + AVED).
A project can add its own backend as `module:ClassName` in a project-local
`gwbackends/` directory, the same extension point `testdir` uses for
`target_bridge.py`/`tb_base.py`.

### AXI dispatchers and shared configuration

The synthesizable Chisel side provides reusable modules for DUT-specific
control/status registers, queues, memories, and test sequencing, built on
the AXI utilities below. A JSON file generated from the Chisel side keeps
design parameters and the AXI address map consistent across the DUT,
dispatcher, and cocotb testbench.

See [garageworks.md](garageworks.md) for Maven/PyPI package info.

## AXI utilities for Chisel

Tested on Chisel 7.9.0 and Verilator 5.044.

The AXI utilities are not a full-featured AXI reference implementation.
They provide minimal and clean interfaces, practical subsets of the
protocol, and helpers that simplify testbench development.

### AXI port bundles

Predefined AXI bundles that can be directly instantiated in your Chisel
modules:

```scala
class MyModule(AxiAddrBW: Int = 24) extends Module {
  val io = IO(new Bundle {
    val axi = new AxiLite32IO(AxiAddrBW)
  })
}
```

### Simplified bus functional models (BFMs)

Example usage of the higher-level interface:

```scala
"test MyModule" should "pass" in {
  simulate(new MyModule) { dut =>
    val bfm = new Axi4Lite32BFM(dut)
    bfm.initMaster()
    val bresp = bfm.write(0x10, 0x123L)  // write 0x123 to the address 0x10
    val (rdata, rresp) = bfm.read(0x10)  // read the address 0x10
    ...
  }
}
```

Lower-level channel methods are also available when finer control is needed:
`sendAW(...)`, `sendW(...)`, `sendSimulAWW(...)`, `recvB(...)`,
`sendAR(...)`, `recvR(...)`.

## Dependencies

### Linux distro

Tested with Ubuntu 24.04.4 LTS and Fedora 41 and 42. Any newer major Linux
distro is expected to work.

### SBT

This project pins sbt 1.11.7 (see `project/build.properties`). sbt 2.x is
not used yet because the Maven Central publishing plugins (`sbt-sonatype`,
`sbt-pgp`) do not have a stable sbt 2.x build. The installed `sbt` launcher
automatically fetches and uses the pinned version.

### Verilator

Verilator 5.044 has been tested. `sh misc/build_verilator.sh INSTDIR` builds
and installs it locally; add `INSTDIR/bin` to `PATH`.

### FPGA environment (Vivado/Vitis, AMD Alveo V80 + AVED)

`fpgatiming`/`aved`/`buildhw`/`programhw` need Vivado/Vitis 2025.1 and the
AVED Arm cross compiler on `PATH`, e.g.:

```
export XILINXD_LICENSE_FILE=...
source $INSTDIR/2025.1/Vitis/settings64.sh
export PATH="$INSTDIR/2025.1/gnu/armr5/lin/gcc-arm-none-eabi/bin:$PATH"
```

`programhw` also needs passwordless sudo for the PCIe rescan and `ami_tool`
(`gwscript --sudoers` prints the line to add). See `python/garageworks/gwscript/config.py`'s
module docstring for the full per-stage tool list.

## Roadmap

- AI-assisted generation of DUT-specific test components (AXI dispatchers,
  address maps, Chisel/cocotb testbenches)
- A polished FPGA driver build script
- Additional examples (e.g., streaming data feeder and receiver)
- AXI4-Stream utilities and partial AXI4-Full support
- Support for additional FPGA platforms
