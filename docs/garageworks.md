# GarageWorks

GarageWorks is a lightweight FPGA testing framework that lets the same cocotb
testbench run on both software RTL simulation and FPGA hardware. A shared
bridge abstraction hides backend-specific interfaces, while synthesizable AXI
dispatchers written in Chisel provide design-under-test (DUT)-specific control
and test operations on the FPGA. It targets rapid bare-metal testing of
standalone designs and on-chip processing logic (e.g., scientific sensors),
rather than full SoC-level prototyping. The current FPGA backend targets the
AMD Alveo V80 with a modified AVED environment.

See [tutorials.md](tutorials.md) for how to test your own design, and
[development-notes.md](development-notes.md) for GarageWorks' internals.

## Chisel library (Maven)

The Chisel library (`io.github.hwspec::garageworks`, see [build.sbt](../build.sbt)
for the current `-SNAPSHOT` version) is published to Sonatype Central's Maven
snapshot repository, so other projects can depend on it without a local `sbt
publishLocal`. Add the snapshot resolver and the dependency to your own
`build.sbt`:

```scala
resolvers += "central-snapshots" at "https://central.sonatype.com/repository/maven-snapshots/"

libraryDependencies += "io.github.hwspec" %% "garageworks" % "0.4.0-SNAPSHOT"
```

## Python package (PyPI)

`gwscript` and the cocotb/AVED bridges are published to PyPI. Only dev
(`.devN`) releases exist so far (`N` derived from the commit they were built
from), so install without pinning a version; pip resolves to the latest one
automatically since there's no stable release to prefer over it:

```
pip install garageworks
```

See [PyPI](https://pypi.org/project/garageworks/#history) for the release
history.

## Example project

[SRV32I](https://github.com/hwspec/SRV32I) is a small RISC-V core that
demonstrates the full GarageWorks workflow, from RTL generation and cocotb
simulation through FPGA testing. See the main [README](../README.md) for how
to run it.
