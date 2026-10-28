# gwproj-example

[`gwproj-example`](https://github.com/hwspec/gwproj-example) is a small,
from-scratch GarageWorks project. It was generated from
[chipsalliance/chisel-template](https://github.com/chipsalliance/chisel-template)
(via GitHub's "Use this template" button, so it starts from a standard,
already-working Chisel3 + sbt/mill skeleton), then extended with:

- an AXI4-Lite command dispatcher, `AxiGCD` (the same bridging pattern as
  GarageWorks' own `GWCmdTemplate` — see
  [tutorial-chisel-dut.md](tutorial-chisel-dut.md)) that wraps
  chisel-template's `DecoupledGcd` example design,
- a cycle counter that measures how many clock cycles each GCD computation
  takes,
- a `gwtests/` cocotb harness and `.gwconfig` so the same testbench can run
  under `gwscript`.

It's meant as a second, smaller worked example alongside
[SRV32I](https://github.com/hwspec/SRV32I): SRV32I shows a full real core,
`gwproj-example` shows the minimum scaffolding needed to go from a plain
Chisel module to an AXI-Lite-addressable, cocotb-testable GarageWorks target.

## Clone and run

```sh
git clone https://github.com/hwspec/gwproj-example.git
cd gwproj-example
```

Run the Chisel-level tests (ChiselSim, via Verilator):

```sh
sbt test
```

This runs both `GCDSpec` (chisel-template's original direct test of
`DecoupledGcd`) and `AxiGCDSpec` (the new AXI-Lite-driven test of `AxiGCD`).
`build.mill` (from chisel-template, extended the same way as `build.sbt`)
works too:

```sh
./mill gwproj-example.test
```

Run the cocotb simulation test (needs `pip install garageworks` for the
cocotb/AXI bridge, plus Verilator):

```sh
gwscript
```

`gwscript` reads `.gwconfig` in the project root:

```
chiseltop=axigcd.AxiGCD
testdir=gwtests
tests=base
```

— it generates RTL for `axigcd.AxiGCD`, then runs the cocotb test(s) named
in `tests` (here, `gwtests/tb_base.py`). You can drive the same thing
directly with `make`:

```sh
cd gwtests
make -f Makefile.gw base
```

## Chisel sources

- **`src/main/scala/gcd/GCD.scala`** — unmodified from chisel-template: a
  plain sequential GCD-by-subtraction module. `loadingValues` pulses high to
  latch `value1`/`value2`; `outputValid` goes high once `y` reaches zero.
  Not used by `AxiGCD`; kept as chisel-template's original example.

- **`src/main/scala/gcd/DecoupledGcd.scala`** — also unmodified from
  chisel-template: the same subtraction algorithm, but wrapped in a
  ready/valid (`Decoupled`) handshake — `input: Decoupled[GcdInputBundle]`,
  `output: Decoupled[GcdOutputBundle]`. This is the module `AxiGCD` actually
  instantiates as its DUT, because its handshake maps naturally onto
  "write operands, poll for done."

- **`src/main/scala/axigcd/AxiGCD.scala`** — the new piece: an AXI4-Lite
  bridge around `DecoupledGcd`, modeled directly on GarageWorks'
  `GWCmdTemplate.scala`. `AxiGcdParams` is both the RTL parameter case class
  and the register map (its fields are serialized to
  `generated/AxiGCD/params.json`, which both the Scala spec and the cocotb
  bridge read back). Register map:

  | Address | Field          | R/W | Meaning |
  |---------|----------------|-----|---------|
  | `0x00`  | `soft_reset_rw`| R/W | write `1` to soft-reset `DecoupledGcd` and the GCD latches; reads `1` once the reset has completed |
  | `0x10`  | `const1_r`     | R   | module-ID constant, for sanity-checking the bitstream |
  | `0x20`  | `const2_r`     | R   | git hash of the generating commit |
  | `0x30`  | `value1_rw`    | R/W | first GCD operand |
  | `0x40`  | `value2_rw`    | R/W | second GCD operand; **writing this latches both operands and starts the computation** |
  | `0x50`  | `result_r`     | R   | `gcd(value1, value2)`, valid once `valid_r` reads `1` |
  | `0x60`  | `valid_r`      | R   | `1` once `result_r` holds a valid result |
  | `0x70`  | `gcd_cycles_r` | R   | clock cycles the most recently started computation took |

  The cycle counter is two registers: `gcdCycleCounterReg` is set to `1` the
  cycle `value2_rw` is written (the same cycle `DecoupledGcd`'s input fires)
  and increments every cycle while a `busyReg` flag is set; `gcdCyclesReg`
  latches a copy of the counter on `dut.output.fire` (when the result becomes
  valid) and is what `gcd_cycles_r` exposes. Both, along with `dut` itself
  and the operand/result latches, live inside a `withReset(combinedReset)`
  block, so a soft reset clears them the same way it clears `GWCmdTemplate`'s
  `Dut` — a stale cycle count or result can't leak across a reset.

- **`src/test/scala/gcd/GCDSpec.scala`** — unmodified from chisel-template:
  pokes `DecoupledGcd`'s ports directly (no AXI) across a 0..10 × 0..10
  sweep of operand values.

- **`src/test/scala/axigcd/AxiGCDSpec.scala`** — the new spec: uses
  GarageWorks' `Axi4Lite32BFM` to drive `AxiGCD`'s AXI-Lite port the way real
  hardware would. `runGcdTest(bfm, value1, value2)` writes the operands,
  polls `valid_r`, checks `result_r` against a Scala `gcd()` reference, and
  reads back `gcd_cycles_r` for a log line. A table of test vectors (swapped
  operand order, zero, equal values, a multi-step pair) is run through it in
  a single `simulate` block, followed by a soft-reset check.

## gwtests/ (cocotb harness)

Mirrors the pattern described in
[tutorial-chisel-dut.md](tutorial-chisel-dut.md):

- **`.gwconfig`** — tells `gwscript` which Chisel module to generate
  (`chiseltop`), where the testbenches live (`testdir`), and which cocotb
  test target(s) to run (`tests`).
- **`gwtests/Makefile.gw`** — the shared, repo-agnostic cocotb/FPGA Makefile
  (same file as GarageWorks' own `gwtests/Makefile`).
- **`gwtests/gcd.mk`** — the repo-specific settings `Makefile.gw` includes:
  module name, bridge module name, top name, and the `sbt runMain` command
  that (re)generates RTL.
- **`gwtests/gcd_bridge.py`** — a thin `COCOTB_Bridge` subclass
  (`GcdBridge`); the base class already provides `writeWord`/`readWord`/
  `expectWord`/`softReset`/`setup`, so this file is just a place to add
  `AxiGCD`-specific helpers later if needed.
- **`gwtests/tb_base.py`** — the cocotb test itself: the Python mirror of
  `AxiGCDSpec.scala`, using the same test vectors and `math.gcd` as its
  reference, so the ChiselSim run and the Verilator/cocotb run can be
  cross-checked against each other.

## How to add a new param (register)

`AxiGcdParams`'s fields double as both Chisel's RTL parameters and the
AXI-Lite register map, so adding one touches a few places consistently:

1. **Add the field** to the `AxiGcdParams` case class (no default value —
   see the comment at the top of the class; this forces every call site to
   be explicit).
2. **Give it an address** in `AxiGcdParams.default`. Addresses here are
   4-byte-aligned byte offsets with no particular spacing requirement beyond
   staying unique and 4-byte aligned (this example uses `0x10` steps for
   readability).
3. **If it's writable**, add an `.elsewhen(a === p.your_field.U) { ... }`
   branch in the write FSM (inside `when(doWrite) { ... }`, alongside
   `value1_rw`/`value2_rw`), assigning from `wHoldDataReg`.
4. **If it's readable**, add a matching `.elsewhen(araddr === p.your_field.U)
   { rdataReg := ...; rstate := RState.COMPLETED }` branch in the AR/R
   read-decode chain.
5. **Pick the right reset domain**: if the value should reset whenever the
   GCD state does (like `gcdCyclesReg`), declare its register inside the
   `withReset(combinedReset) { ... }` block; if it's bus/protocol state that
   should only reset at power-on (like the AXI handshake registers),
   declare it at the top level of the module instead.
6. **Regenerate RTL and `params.json`**: `sbt "runMain axigcd.AxiGCD"` (or
   just run `sbt test` / `gwscript` — `gwtests/gcd.mk`'s rule for
   `$(SRCDIR)/AxiGCD.sv` reruns this automatically whenever a `.scala` file
   under `src/` changes).
7. **Use it**: in Scala, `p.your_field`, as `AxiGCDSpec.scala` does; in
   cocotb, `dut.p.your_field` — both read the same `params.json`.
8. **Re-run both test suites** (`sbt test`, and `make -f Makefile.gw base`
   from `gwtests/`) to confirm the new register round-trips correctly end
   to end.

Since `AxiGcdParams` fields have no defaults, forgetting to pass one to
`AxiGcdParams.default` (or to an inline construction) is a compile error,
not a silent bug — a small but useful guardrail when extending the register
map.
