# GarageWorks

> **Status: alpha release.** APIs, directory layout, and build flow may change.

GarageWorks is a lightweight FPGA testing framework that lets the same cocotb
testbench run on both software RTL simulation and FPGA hardware. A shared
bridge abstraction hides backend-specific interfaces, while synthesizable AXI
dispatchers written in Chisel provide design-under-test (DUT)-specific control
and test operations on the FPGA.

GarageWorks targets rapid bare-metal testing of standalone designs and on-chip
processing logic (e.g., for scientific sensors), rather than full SoC-level
prototyping. The current FPGA backend targets the AMD Alveo V80 with a
modified AVED environment.

GarageWorks started from [chisel-axi-utils](https://github.com/hwspec/chisel-axi-utils),
which provided lightweight AXI utilities and bus-functional models (BFMs) for
Chisel. That repository will be archived; new development continues here.

Note: Instructions for testing on the V80 FPGA are not documented yet, but
will be provided soon.

## Overview

- **Shared testbench abstraction:** Write a cocotb testbench once and run it
  in RTL simulation or on the FPGA.
- **Backend bridges:** `cocotb_bridge` for software RTL simulation and
  `aved_bridge` for FPGA execution through `pyaved`, exposing common
  primitives such as soft reset, AXI read/write, and expected-value checks.
- **Source-to-source translator:** Converts cocotb testbench code into the
  FPGA-targeted form, so the original testbench needs no manual modification.
- **Synthesizable AXI dispatchers:** Reusable Chisel modules for DUT-specific
  control/status registers, queues, memories, and test sequencing.
- **Shared configuration:** A JSON file generated from the Chisel side keeps
  design parameters and the AXI address map consistent across the DUT,
  dispatcher, and testbench.
- **AXI utilities:** AXI4-Lite port bundles and simplified BFMs for Chisel.

## Dependencies

### Linux distro

We have tested it with Ubuntu 24.04.4 LTS and Fedora 41. We believe that any
newer major Linux distro works.

### JDK 8 or newer

We recommend LTS releases Java 8 and Java 11. You can install the JDK as your
operating system recommends, or use the prebuilt binaries from
[AdoptOpenJDK](https://adoptopenjdk.net/).

### SBT

SBT is the most common build tool in the Scala community. You can download it
[here](https://www.scala-sbt.org/download.html).

### Verilator

Chisel and cocotb require Verilator installed. Verilator 5.044 has been tested.

To build and install it locally:

```
sh misc/build_verilator.sh INSTDIR
```

NOTE: add INSTDIR/bin to PATH

### cocotb

Tested with Python 3.8+.

To set up a Python virtual environment and install required packages:

```
make setup
```

## Examples

The following files can be used as templates for your project:

- `src/main/scala/axi_examples/Axi4Lite32Cmd.scala`: a Chisel module example
  for bridging with your DUT. It includes soft reset logic.
- `src/test/scala/axi_examples/Axi4Lite32CmdSpec.scala`: a Chisel testbench
  for `Axi4Lite32Cmd`.
- `tests/Cmd/{CmdSim.py, sim_simple.py, Makefile}`: a cocotb testbench for
  `Axi4Lite32Cmd`. This testbench can be converted to an FPGA testbench on
  AMD V80 AVED (modified version) without modification.

## AXI Utilities for Chisel

Tested on Chisel 7.9.0 and Verilator 5.044.

The AXI utilities are not a full-featured AXI reference implementation.
They provide minimal and clean interfaces, practical subsets of the protocol,
and helpers that simplify testbench development.

### AXI Port Bundles

Predefined AXI bundles that can be directly instantiated in your Chisel
modules:

```scala
class MyModule(AxiAddrBW: Int = 24) extends Module {
  val io = IO(new Bundle {
    val axi = new AxiLite32IO(AxiAddrBW)
  })
}
```

### Simplified Bus Function Models (BFMs)

Example usage of the higher-level interface:

```scala
"test AxiList32RevMem" should "pass" in {
  simulate(new Axi4Lite32RevMem) { dut =>
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

## Roadmap

- AI-assisted generation of DUT-specific test components (AXI dispatchers,
  address maps, Chisel/cocotb testbenches)
- V80 FPGA test instructions and a polished FPGA driver build script
- Additional examples (e.g., streaming data feeder and receiver)
- AXI4-Stream utilities and partial AXI4-Full support
- Support for additional FPGA platforms

## License

BSD. See [LICENSE](LICENSE).

## Contact

For questions, bug reports, or feature requests, please open an issue on
GitHub. For general discussion, please use GitHub Discussions.
