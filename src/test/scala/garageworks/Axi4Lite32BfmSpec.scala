// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package garageworks

import chisel3._
import chisel3.util._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.flatspec.AnyFlatSpec

// Minimal AXI4-Lite register-file DUT used only to exercise Axi4Lite32BFM
// directly, independent of any specific peripheral (e.g. cmdtemplate.GWCmdTemplate).
private object BfmTestDutParams extends AxiModuleParams with AxiModuleDefParams {
  val moduleName = "BfmTestDut"
  val soft_reset_rw: Long = 0x0
  val regCount = 4
  def regAddr(i: Int): Long = 0x4 + 4 * i
}

private class BfmTestDut extends Module with HasAxiLite32IO {
  import BfmTestDutParams._
  override val S = IO(new AxiLite32IO())
  val axi = S.AXI

  val regs = RegInit(VecInit(Seq.fill(regCount)(0.U(32.W))))

  val resetCounterReg = RegInit(0.U(8.W))
  val softResetDoneReg = RegInit(true.B)
  when(resetCounterReg > 0.U) {
    resetCounterReg := resetCounterReg - 1.U
  }.otherwise {
    softResetDoneReg := true.B
  }

  // -----------------------------
  // Write path: AW/W -> B
  // -----------------------------
  val awHoldValidReg = RegInit(false.B)
  val awHoldAddrReg = Reg(UInt(32.W))
  val wHoldValidReg = RegInit(false.B)
  val wHoldDataReg = Reg(UInt(32.W))
  val wHoldStrbReg = Reg(UInt(4.W))
  val bValidReg = RegInit(false.B)
  val bRespReg = RegInit(0.U(2.W))

  axi.awready := !awHoldValidReg && !bValidReg
  axi.wready := !wHoldValidReg && !bValidReg

  when(axi.awvalid && axi.awready) {
    awHoldAddrReg := axi.awaddr
    awHoldValidReg := true.B
  }
  when(axi.wvalid && axi.wready) {
    wHoldDataReg := axi.wdata
    wHoldStrbReg := axi.wstrb
    wHoldValidReg := true.B
  }

  val doWrite = awHoldValidReg && wHoldValidReg && !bValidReg
  when(doWrite) {
    val addr = awHoldAddrReg
    val bresp = WireDefault(AxiLiteResp.OKAY.U(2.W))

    when(addr === soft_reset_rw.U) {
      resetCounterReg := 8.U
      softResetDoneReg := false.B
    }.elsewhen(addr >= regAddr(0).U && addr < regAddr(regCount).U) {
      val idx = ((addr - regAddr(0).U) >> 2.U)(log2Ceil(regCount) - 1, 0)
      val oldData = regs(idx)
      val newBytes = (0 until 4).map { i =>
        Mux(wHoldStrbReg(i), wHoldDataReg(8 * i + 7, 8 * i), oldData(8 * i + 7, 8 * i))
      }
      regs(idx) := Cat(newBytes.reverse)
    }.otherwise {
      bresp := AxiLiteResp.SLVERR.U
    }

    bRespReg := bresp
    bValidReg := true.B
    awHoldValidReg := false.B
    wHoldValidReg := false.B
  }
  when(bValidReg && axi.bready) {
    bValidReg := false.B
  }
  axi.bvalid := bValidReg
  axi.bresp := bRespReg

  // -----------------------------
  // Read path: AR -> R
  // -----------------------------
  object RState extends ChiselEnum {
    val READY, DONE = Value
  }
  val rstateReg = RegInit(RState.READY)
  val rdataReg = Reg(UInt(32.W))
  val rrespReg = RegInit(0.U(2.W))

  axi.arready := rstateReg === RState.READY
  axi.rvalid := rstateReg === RState.DONE
  axi.rdata := rdataReg
  axi.rresp := rrespReg

  when(axi.arvalid && axi.arready) {
    val addr = axi.araddr
    val rresp = WireDefault(AxiLiteResp.OKAY.U(2.W))
    val rdata = WireDefault(0.U(32.W))

    when(addr === soft_reset_rw.U) {
      rdata := softResetDoneReg.asUInt
    }.elsewhen(addr >= regAddr(0).U && addr < regAddr(regCount).U) {
      val idx = ((addr - regAddr(0).U) >> 2.U)(log2Ceil(regCount) - 1, 0)
      rdata := regs(idx)
    }.otherwise {
      rresp := AxiLiteResp.SLVERR.U
    }

    rdataReg := rdata
    rrespReg := rresp
    rstateReg := RState.DONE
  }
  when(rstateReg === RState.DONE && axi.rready) {
    rstateReg := RState.READY
  }
}

class Axi4Lite32BfmSpec extends AnyFlatSpec with ChiselSim {
  import Axi4WriteMode._
  import BfmTestDutParams._

  "Axi4Lite32BFM" should "round-trip a value using the default (same-cycle) write mode" in {
    simulate(new BfmTestDut) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      val resp = bfm.write(regAddr(0), 0x12345678L)
      assert(resp == AxiLiteResp.OKAY)
      assert(bfm.readVal(regAddr(0)) == BigInt(0x12345678L))
    }
  }

  it should "round-trip values using AWFirst and WFirst write ordering" in {
    simulate(new BfmTestDut) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      bfm.write(regAddr(1), 0xAAAAAAAAL, mode = AWFirst)
      bfm.expectVal(regAddr(1), 0xAAAAAAAAL)

      bfm.write(regAddr(2), 0x55555555L, mode = WFirst)
      bfm.expectVal(regAddr(2), 0x55555555L)
    }
  }

  it should "only update the bytes selected by wstrb" in {
    simulate(new BfmTestDut) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      bfm.writeVal(regAddr(0), 0xAABBCCDDL)
      bfm.write(regAddr(0), 0x000000FFL, strb = 0x1) // update only byte 0
      bfm.expectVal(regAddr(0), 0xAABBCCFFL)
    }
  }

  it should "return SLVERR for writes and reads to unmapped addresses" in {
    simulate(new BfmTestDut) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      val wResp = bfm.write(0xDEAD0000L, 0x1)
      assert(wResp == AxiLiteResp.SLVERR)

      val (_, rResp) = bfm.read(0xDEAD0000L)
      assert(rResp == AxiLiteResp.SLVERR)
    }
  }

  it should "support stalling bready/rready via stallBReady/stallRReady" in {
    simulate(new BfmTestDut) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      val wResp = bfm.write(regAddr(3), 0x42, stallBReady = 5)
      assert(wResp == AxiLiteResp.OKAY)

      val (data, rResp) = bfm.read(regAddr(3), stallRReady = 5)
      assert(data == BigInt(0x42))
      assert(rResp == AxiLiteResp.OKAY)
    }
  }

  it should "support the softReset convenience helper" in {
    simulate(new BfmTestDut) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      bfm.softReset(BfmTestDutParams)
      bfm.expectVal(BfmTestDutParams.soft_reset_rw, 1)
    }
  }
}
