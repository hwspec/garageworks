# Tutorial: wrapping a Chisel DUT with GarageWorks

End-to-end walkthrough of taking a plain Chisel module and making it a
GarageWorks target: write the DUT and its Chisel-level test as an ordinary
sbt project, add GarageWorks, generate an AXI4-Lite command dispatcher
around it with `gwscript --gwcd`, then run it through simulation (and,
optionally, FPGA).

The example DUT is a simple synchronous FIFO: 32-bit words (`BW`),
parameterized depth (16 entries here). Once wrapped by `--gwcd`, writing a
word pushes it into the FIFO at one AXI-Lite address, and reading from
another address pops the next word out — i.e. plain read/write access to a
specific address, which is exactly what AXI4-Lite gives you for free.

## 1. A plain Chisel FIFO

Start as an ordinary, GarageWorks-free Chisel project. The finished project
from this section is available as a zip, if you'd rather start from that than
type it out: [fifo-example.zip](assets/fifo-example.zip).

```
fifo-example/
├── build.sbt
├── project/
│   └── build.properties
└── src/
    ├── main/scala/fifo/Fifo.scala
    └── test/scala/fifo/FifoSpec.scala
```

**`src/main/scala/fifo/Fifo.scala`**

```scala
// See README.md for license details.
package fifo

import chisel3._
import chisel3.util.Queue

/** Simple synchronous FIFO: `bw`-bit words, `depth` entries, built on chisel3.util.Queue. */
class Fifo(val bw: Int = 32, val depth: Int = 16) extends Module {
  val io = IO(new Bundle {
    val wrEn   = Input(Bool())
    val wrData = Input(UInt(bw.W))
    val rdEn   = Input(Bool())
    val rdData = Output(UInt(bw.W))
    val empty  = Output(Bool())
    val full   = Output(Bool())
  })

  val q = Module(new Queue(UInt(bw.W), depth))
  q.io.enq.valid := io.wrEn
  q.io.enq.bits  := io.wrData
  q.io.deq.ready := io.rdEn

  io.full  := !q.io.enq.ready
  io.empty := !q.io.deq.valid

  val rdDataReg = RegInit(0.U(bw.W))
  when(io.rdEn && q.io.deq.valid) {
    rdDataReg := q.io.deq.bits
  }
  io.rdData := rdDataReg
}
```

**`src/test/scala/fifo/FifoSpec.scala`**

```scala
// See README.md for license details.
package fifo

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec

class FifoSpec extends AnyFreeSpec with ChiselSim {
  "Fifo should write, read, and report empty/full correctly" in {
    simulate(new Fifo(bw = 32, depth = 16)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.clock.step()
      assert(dut.io.empty.peek().litToBoolean)
      assert(!dut.io.full.peek().litToBoolean)

      // write one word, then read it back
      dut.io.wrEn.poke(true.B)
      dut.io.wrData.poke(0x1234.U)
      dut.clock.step()
      dut.io.wrEn.poke(false.B)
      assert(!dut.io.empty.peek().litToBoolean)

      dut.io.rdEn.poke(true.B)
      dut.clock.step()
      dut.io.rdEn.poke(false.B)
      dut.io.rdData.expect(0x1234.U)
      assert(dut.io.empty.peek().litToBoolean)

      // fill the FIFO completely and check full
      dut.io.wrEn.poke(true.B)
      for (i <- 0 until 16) {
        dut.io.wrData.poke(i.U)
        dut.clock.step()
      }
      dut.io.wrEn.poke(false.B)
      assert(dut.io.full.peek().litToBoolean)

      // drain it back out, in write order
      dut.io.rdEn.poke(true.B)
      for (i <- 0 until 16) {
        dut.clock.step()
        dut.io.rdData.expect(i.U)
      }
      dut.io.rdEn.poke(false.B)
      assert(dut.io.empty.peek().litToBoolean)

      // soft reset: rdData must read back as zero
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.rdData.expect(0.U)
      assert(dut.io.empty.peek().litToBoolean)
    }
  }
}
```

**`build.sbt`** (pure Chisel, no GarageWorks yet)

```scala
// See README.md for license details.

ThisBuild / scalaVersion := "2.13.18"
ThisBuild / version      := "0.1.0"

val chiselVersion = "7.13.0"

lazy val root = (project in file("."))
  .settings(
    name := "fifo-example",
    libraryDependencies ++= Seq(
      "org.chipsalliance" %% "chisel" % chiselVersion,
      "org.scalatest" %% "scalatest" % "3.2.19" % "test",
    ),
    scalacOptions ++= Seq(
      "-language:reflectiveCalls",
      "-deprecation",
      "-feature",
      "-Xcheckinit",
      "-Ymacro-annotations",
    ),
    addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full),
  )
```

**`project/build.properties`**

```
sbt.version = 1.12.4
```

### Run the Chisel-level test

Needs Verilator (`ChiselSim`'s default backend):

```sh
sbt test
```

## 2. Install GarageWorks and wire it into the build

```sh
pip install "garageworks[gwcd]"   # [gwcd] pulls in the LLM clients used by --gwcd below
```

`--gwcd` checks whether `build.sbt` already depends on `garageworks` and
appends the resolver + dependency itself if not, so you can skip straight to
step 3. If you'd rather add it by hand now, inside `fifo-example`'s
`.settings(...)`:

```scala
resolvers += "central-snapshots" at "https://central.sonatype.com/repository/maven-snapshots/",

libraryDependencies += "io.github.hwspec" %% "garageworks" % "0.4.0-SNAPSHOT",
```

## 3. Generate the AXI-Lite wrapper with `--gwcd`

```sh
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY
gws --gwcd src/main/scala/fifo/Fifo.scala
```

The filename (`Fifo.scala`) matches the class name (`Fifo`), so no `:TOP` is
needed. `gws --gwcd` then:

1. Asks the LLM for an AXI4-Lite wrapper (`FifoGWCD.scala`) and a ScalaTest
   spec (`FifoGWCDSpec.scala`), following the same pattern as
   `GWCmdTemplate.scala`.
2. Runs `sbt compile` and `sbt test`, retrying (up to 3 attempts) if the
   generated code doesn't build or pass.
3. Since no `.gwconfig` exists yet, finishes by scaffolding it (plus
   `gwtests/Makefile.gw`, `gwtests/fifo.mk`, `gwtests/fifo_bridge.py`,
   `gwtests/tb_base.py`) and prints:

   ```
   Setup complete. Re-run gwscript to start the stages.
   ```

## 4. Double-check with `sbt test`

```sh
sbt test
```

This now runs both `FifoSpec` (the plain Chisel test from step 1) and the
generated `FifoGWCDSpec` (drives `FifoGWCD` over simulated AXI-Lite).

## 5. Run GarageWorks for all simulation steps

```sh
gws
```

Reads the `.gwconfig` written in step 3 (`chiseltop=fifo.FifoGWCD`), runs
`prereq -> rtlgen -> cocotb`: regenerates Verilog for `FifoGWCD`, then runs
the cocotb test(s) named in `tests=` against it under Verilator — the same
checks as `FifoGWCDSpec.scala`, exercised through the cocotb/AXI bridge
instead of ChiselSim.

## 6. Run it on FPGA

```sh
gws --fpga --platform aved
```

(or persist it with `gws --enable-fpga` so plain `gws` always includes the
hardware stages). This runs `aved -> buildhw -> programhw -> testhw` against
a real AMD Alveo V80 board with the modified AVED environment — currently
the only supported FPGA backend. See
[development-status.md](development-status.md) for what's verified there.
