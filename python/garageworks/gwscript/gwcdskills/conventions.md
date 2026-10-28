# GWCD conventions

A GarageWorks Command Dispatcher (GWCD) is an AXI4-Lite wrapper Chisel
module around a DUT. Every GWCD you generate must follow these rules.

## Params case class = register map

- The params case class (e.g. `AxiGcdParams`) has **no default values** on
  its fields — this forces every construction site to be explicit. Fields
  whose name ends in `_r`, `_w`, or `_rw` are AXI-Lite register addresses
  (byte offsets); other fields are plain RTL parameters or constants.
- The companion object's `default(const1, const2)` method assigns the
  actual address values. Convention (not a hard requirement beyond 4-byte
  alignment and uniqueness): `soft_reset_rw = 0x00`, `const1_r = 0x10`,
  `const2_r = 0x20`, then further registers in `0x10` steps.
- `soft_reset_rw` (R/W): writing `1` soft-resets the DUT; reads `1` once the
  reset has completed (see the soft-reset logic shared by both canonical
  examples — `softResetReg`/`softResetDoneReg`/`resetCounterReg` /
  `combinedReset`, copy it verbatim).
- `const1_r` / `const2_r` (R): `const1` is a module-ID sanity constant
  (pick any fixed nonzero `Long`, e.g. `0xa415c0dL`); `const2` is always
  `getGitHash` from `garageworks.AxiModuleParamsHelper._`.
- Mark the class with `extends AxiModuleParams with AxiModuleDefParams` and
  give it a `val moduleName = "..."`.
- Give the companion object `implicit val rw: ReadWriter[YourParams] =
  macroRW` (from `upickle.default._`) so params round-trip to
  `generated/<Top>/params.json`.

## Write path (AW/W -> B)

Inside `when(doWrite) { ... }` (copy the AW/W handshake and `doWrite`
computation verbatim from the canonical examples), decode the held address:

- `!fullWrite` (anything other than a full 4-byte `wstrb`) -> `bresp :=
  SLVERR.U`. Only full-word writes are supported.
- `a === p.soft_reset_rw.U` -> trigger the soft reset, `bresp := OKAY.U`.
- One `.elsewhen(a === p.your_field_rw.U) { ... }` branch per writable
  register, assigning from `wHoldDataReg`.
- `.otherwise { bresp := AxiLiteResp.SLVERR.U }` for any other address —
  writes to unmapped/read-only addresses get `SLVERR`.

## Read path (AR -> R)

Inside `when(arFire) { ... }` (copy the AR handshake and `RState`
enum/`rstateReg` verbatim), decode `araddr = S.AXI.araddr(19, 0)` (truncate
to 20 bits — the MMIO range is up to 1MB):

- One `.elsewhen(araddr === p.your_field.U) { rdataReg := ...; rstate :=
  RState.COMPLETED }` branch per readable register (including
  `const1_r`/`const2_r`/`soft_reset_rw`).
- `.otherwise { rdataReg := 0xbad00000L.U | S.AXI.araddr(31, 0); rstate :=
  RState.COMPLETED }` for any unmapped address.

**Do not use `SLVERR` on the read path** — unlike writes, bad reads return
the `0xbad00000 | araddr` sentinel value with `OKAY`. This asymmetry is
deliberate: the AVED FPGA host's read path only tolerates `OKAY` responses,
so `SLVERR` on a read would break real hardware even though it works in
simulation.

## Reset wiring: two supported patterns

1. **`extmodule_direct`** — the DUT is a Verilog module wrapped as an
   `ExtModule`/`BlackBox` (see canonical example 1, `Dut`/`GWCmdTemplate`):
   `val dut = Module(new Dut(...)); dut.clock := clock; dut.reset :=
   combinedReset`. Inputs/outputs are driven/read directly as top-level
   `val`s outside any `withReset` block.
2. **`withreset_wrap`** — the DUT is a plain Chisel `Module`/`RawModule`
   with an implicit reset port (see canonical example 2, `AxiGCD`/
   `DecoupledGcd`): instantiate it, and every register that should clear on
   a soft reset, inside `withReset(combinedReset) { val dut = Module(new
   X(...)); ...; (dut, ...) }`, returning whatever values are needed outside
   the block as a tuple.

Use exactly the pattern named in the "Target" section of the task — do not
mix the two or invent a third.

## Companion `object X extends App`

Every GWCD file ends with:

```scala
object YourGWCD extends App {
  val const1 : Long = 0x........L // pick a unique module-id constant
  val const2 : Long = getGitHash

  val p = checkParamEnv(
    YourGwcdParams.default(const1 = const1, const2 = const2),
    "YOURGWCD_MODULE_PARAMS")

  EmitVerilog.generate(new YourGWCD(p, debugprint=true), p)
}
```

`checkParamEnv`/`getGitHash`/`EmitVerilog.generate` all come from
`garageworks.AxiModuleParamsHelper._` / `garageworks._` (already imported
at the top of both canonical examples — copy those imports verbatim).
