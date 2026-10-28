// See README.md for license details.
package axigcd

import garageworks._
import garageworks.AxiLiteResp._
import garageworks.AxiModuleParamsHelper._
import upickle.default._

import chisel3._
import chisel3.util._

import gcd.DecoupledGcd

case class AxiGcdParams( // Note: do not put default value here
                         // params suffixed with _r, _w, or _rw represent addresses
                         // DefParams
                         soft_reset_rw : Long,
                         // module params
                         const1_r : Long, const2_r : Long,
                         value1_rw : Long, value2_rw : Long,
                         result_r : Long, valid_r : Long, gcd_cycles_r : Long,
                         // constant definition
                         const1: Long, const2: Long,
                         width: Int = 32,
                         reset_cycles : Int, // soft reset cycles
                       ) extends AxiModuleParams with AxiModuleDefParams
{
  val moduleName = "AxiGcd"
}

object AxiGcdParams {
  implicit val rw: ReadWriter[AxiGcdParams] = macroRW

  def default(const1: Long, const2: Long) : AxiGcdParams =
    new AxiGcdParams(
      soft_reset_rw = 0x0, const1_r = 0x10, const2_r = 0x20,
      value1_rw = 0x30, value2_rw = 0x40, result_r = 0x50, valid_r = 0x60,
      gcd_cycles_r = 0x70,
      const1 = const1, const2 = const2, reset_cycles = 8
    )
}

// AXI-Lite bridge around the DecoupledGcd example design: write value1_rw and
// value2_rw (the value2_rw write latches both inputs and kicks off the
// computation), then poll valid_r until it reads 1 and read result_r.
class AxiGCD(p : AxiGcdParams, debugprint: Boolean = false)
  extends chisel3.Module with HasAxiLite32IO {

  override val S = IO(new AxiLite32IO())

  // cycle counter for convenience
  val (cycles, wrap) = Counter(true.B, 1 << 16)

  // soft reset handling logic for dut
  val softResetReg = RegInit(false.B)
  val softResetDoneReg = RegInit(false.B)
  val resetCounterReg = RegInit(0.U(log2Ceil(p.reset_cycles).W))
  when(resetCounterReg > 0.U) {
    resetCounterReg := resetCounterReg - 1.U
  }.otherwise {
    softResetReg := false.B
    softResetDoneReg := true.B
  }
  val combinedReset = (softResetReg || reset.asBool)

  // dut + its input/result latches all live in the soft-reset domain, so a
  // soft reset clears in-flight/stale GCD state the same way it clears
  // GWCmdTemplate's Dut.
  val (dut, value1Reg, value2Reg, resultReg, validReg, startReg, gcdCyclesReg) = withReset(combinedReset) {
    val dut = Module(new DecoupledGcd(p.width))
    val value1Reg = RegInit(0.U(p.width.W))
    val value2Reg = RegInit(0.U(p.width.W))
    val resultReg = RegInit(0.U(p.width.W))
    val validReg = RegInit(false.B)
    val startReg = RegInit(false.B)

    // counts the cycles spent computing the most recently started GCD
    val busyReg = RegInit(false.B)
    val gcdCycleCounterReg = RegInit(0.U(32.W))
    val gcdCyclesReg = RegInit(0.U(32.W))

    dut.input.bits.value1 := value1Reg
    dut.input.bits.value2 := value2Reg
    dut.input.valid := startReg
    dut.output.ready := true.B

    startReg := false.B // one-cycle pulse; (re)asserted on a value2_rw write

    when(startReg) {
      busyReg := true.B
      gcdCycleCounterReg := 1.U
    }.elsewhen(busyReg) {
      gcdCycleCounterReg := gcdCycleCounterReg + 1.U
    }

    when(dut.output.fire) {
      resultReg := dut.output.bits.gcd
      validReg := true.B
      busyReg := false.B
      gcdCyclesReg := gcdCycleCounterReg
    }

    (dut, value1Reg, value2Reg, resultReg, validReg, startReg, gcdCyclesReg)
  }

  // -----------------------------
  // AXI-lite regs
  // -----------------------------
  val awHoldValidReg = RegInit(false.B)
  val awHoldAddrReg = Reg(UInt(p.width.W))
  val wHoldValidReg = RegInit(false.B)
  val wHoldDataReg = Reg(UInt(32.W))
  val wHoldStrbReg = Reg(UInt(4.W))

  val bvalidReg = RegInit(false.B)
  val brespReg = RegInit(0.U(2.W))

  S.AXI.awready := !awHoldValidReg && !bvalidReg
  S.AXI.wready := !wHoldValidReg && !bvalidReg
  val awFire = S.AXI.awvalid && S.AXI.awready
  val wFire = S.AXI.wvalid && S.AXI.wready
  when(awFire) {
    awHoldValidReg := true.B;
    awHoldAddrReg := S.AXI.awaddr(19, 0) // in case MMIO range is 1MB
  }
  when(wFire) {
    wHoldValidReg := true.B
    wHoldDataReg := S.AXI.wdata
    wHoldStrbReg := S.AXI.wstrb
  }

  val doWrite = awHoldValidReg && wHoldValidReg && !bvalidReg
  val addrHoldReg = RegInit(0.U(p.width.W))

  when(doWrite) {
    val a = awHoldAddrReg
    val fullWrite = (wHoldStrbReg === "b1111".U)

    val bresp = WireDefault(OKAY.U)

    when(!fullWrite) { // support full write only for this example
      bresp := SLVERR.U
    }.elsewhen(a === p.soft_reset_rw.U) {
      softResetReg := true.B
      resetCounterReg := p.reset_cycles.U
      softResetDoneReg := false.B
      bresp := OKAY.U
    }.elsewhen(a === p.value1_rw.U) {
      value1Reg := wHoldDataReg
    }.elsewhen(a === p.value2_rw.U) {
      value2Reg := wHoldDataReg
      startReg := true.B
      validReg := false.B
    }.otherwise {
      bresp := AxiLiteResp.SLVERR.U
    }
    brespReg := bresp
    bvalidReg := true.B
    awHoldValidReg := false.B
    wHoldValidReg := false.B
  }

  when(bvalidReg && S.AXI.bready) {
    bvalidReg := false.B
  }
  S.AXI.bvalid := bvalidReg
  S.AXI.bresp := brespReg

  // -----------------------------
  // Read path: AR -> R
  // -----------------------------
  val rdataReg = Reg(UInt(32.W))
  val rrespReg = RegInit(0.U(2.W))

  object RState extends ChiselEnum {
    val READY2READ, COMPLETED = Value
  }

  val rstateReg = RegInit(RState.READY2READ)

  S.AXI.arready := rstateReg === RState.READY2READ
  S.AXI.rvalid := rstateReg === RState.COMPLETED
  S.AXI.rdata := rdataReg
  S.AXI.rresp := rrespReg

  val arFire = S.AXI.arvalid && S.AXI.arready

  when(arFire) {
    if (debugprint) printf("%d: arFire: %x\n", cycles, S.AXI.araddr)
    val araddr = S.AXI.araddr(19, 0) // 1MB range
    rrespReg := OKAY.U

    val rstate = WireDefault(RState.READY2READ)

    when(araddr === p.const1_r.U) {
      rdataReg := p.const1.U
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.const2_r.U) {
      rdataReg := p.const2.U
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.soft_reset_rw.U) {
      rdataReg := softResetDoneReg
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.value1_rw.U) {
      rdataReg := value1Reg
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.value2_rw.U) {
      rdataReg := value2Reg
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.result_r.U) {
      rdataReg := resultReg
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.valid_r.U) {
      rdataReg := validReg
      rstate := RState.COMPLETED
    }.elsewhen(araddr === p.gcd_cycles_r.U) {
      rdataReg := gcdCyclesReg
      rstate := RState.COMPLETED
    }.otherwise {
      if (debugprint) printf("%d: bad read req %d\n", cycles, araddr)
      // rrespReg := SLVERR.U // with this, the host can only read 0xffffffff for any addresses on AVED
      rdataReg := 0xbad00000L.U | S.AXI.araddr(31, 0)
      if (debugprint) printf(cf"arFire otherwise: addr=${S.AXI.araddr}%16x\n")
      rstate := RState.COMPLETED
    }
    rstateReg := rstate
  }

  when(rstateReg === RState.COMPLETED && S.AXI.rready) {
    rstateReg := RState.READY2READ
  }
}

object AxiGCD extends App {
  val const1 : Long = 0xa415c0dL // module id
  val const2 : Long = getGitHash

  val p = checkParamEnv(
    AxiGcdParams.default(const1 = const1, const2 = const2),
    "AXIGCD_MODULE_PARAMS")

  EmitVerilog.generate(new AxiGCD(p, debugprint=true), p)
}
