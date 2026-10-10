// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package axi

import chisel3._

sealed trait AxiWriteMode
object Axi4WriteMode {
  case object SameCycle extends AxiWriteMode
  case object AWFirst   extends AxiWriteMode
  case object WFirst    extends AxiWriteMode
}

class Axi4Lite32BFM[D <: chisel3.Module with axi.HasAxiLite32IO](dut: D, timeoutCycles: Int = 1000) {
  import chisel3.simulator.PeekPokeAPI._
  import Axi4WriteMode._
  
  private def axi = dut.S.AXI

  def step(n: Int = 1): Unit = dut.clock.step(n)

  private def waitUntil(cond: => Boolean, what: String): Unit = {
    var t = 0
    while (!cond) {
      if (t >= timeoutCycles) {
        throw new RuntimeException(s"Timeout waiting for $what after $timeoutCycles cycles")
      }
      step(1)
      t += 1
    }
  }

  private def ok(x: Bool): Boolean = x.peekBoolean()
  private def u(x: UInt): BigInt   = x.peek().litValue

  def initMaster(): Unit = {
    axi.awaddr.poke(0.U);
    axi.awvalid.poke(false.B)
    axi.wdata.poke(0.U);
    axi.wstrb.poke(0.U);
    axi.wvalid.poke(false.B)
    axi.bready.poke(false.B)
    axi.araddr.poke(0.U);
    axi.arvalid.poke(false.B)
    axi.rready.poke(false.B)
    step(1)
  }

  def reset(): Unit = {
    step(1)
    dut.reset.poke(true.B)
    step(1)
    dut.reset.poke(false.B)
    step(1)
  }

  def sendAW(addr: BigInt): Unit = {
    axi.awaddr.poke(addr.U)
    axi.awvalid.poke(true.B)
    waitUntil(ok(axi.awready), "AWREADY")
    step(1) // handshake
    axi.awvalid.poke(false.B)
  }

  def sendW(data: BigInt, strb: Int = 0xF): Unit = {
    axi.wdata.poke(data.U)
    axi.wstrb.poke(strb.U)
    axi.wvalid.poke(true.B)
    waitUntil(ok(axi.wready), "WREADY")
    step(1) // handshake
    axi.wvalid.poke(false.B)
  }

  def sendSimulAWW(addr: BigInt, data: BigInt, strb: Int = 0xF): Unit = {
    axi.awaddr.poke(addr.U)
    axi.wdata.poke(data.U)
    axi.wstrb.poke(strb.U)
    axi.awvalid.poke(true.B)
    axi.wvalid.poke(true.B)
    var awDone = false
    var wDone = false

    while (!awDone || !wDone) {
      val awr = ok(axi.awready)
      val wr = ok(axi.wready)
      if (awr && !awDone) {
        step(1); axi.awvalid.poke(false.B); awDone = true
      }
      if (wr && !wDone) {
        step(1); axi.wvalid.poke(false.B); wDone = true
      }
      if ((!awr || awDone) && (!wr || wDone)) step(1)
    }
  }

  def recvB(stallBReady: Int = 0): Int = {
    if (stallBReady > 0) step(stallBReady)
    axi.bready.poke(true.B)
    waitUntil(ok(axi.bvalid), "BVALID")
    val resp = u(axi.bresp).toInt
    step(1) // handshake
    axi.bready.poke(false.B)
    resp
  }

  def sendAR(addr: BigInt): Unit = {
    axi.araddr.poke(addr.U)
    axi.arvalid.poke(true.B)
    waitUntil(ok(axi.arready), "ARREADY")
    step(1) // handshake
    axi.arvalid.poke(false.B)
  }

  def recvR(stallRReady: Int = 0): (BigInt, Int) = {
    if (stallRReady > 0) step(stallRReady)
    axi.rready.poke(true.B)
    waitUntil(ok(axi.rvalid), "RVALID")
    val data = u(axi.rdata)
    val resp = u(axi.rresp).toInt
    step(1) // handshake
    axi.rready.poke(false.B)
    (data, resp)
  }

  def write(addr: BigInt,
            data: BigInt,
            strb: Int = 0xF,
            mode: AxiWriteMode = SameCycle,
            stallBReady: Int = 0
           ): Int = {

    mode match {
      case SameCycle =>
        sendSimulAWW(addr, data, strb)

      case AWFirst =>
        sendAW(addr)
        sendW(data, strb)

      case WFirst =>
        sendW(data, strb)
        sendAW(addr)
    }

    recvB(stallBReady = stallBReady)
  }

  def read(addr: BigInt, stallRReady: Int = 0): (BigInt, Int) = {
    sendAR(addr)
    recvR(stallRReady = stallRReady)
  }

  // convenient alias. no error checking
  def readVal(addr: Long, offset: Long= 0) : BigInt = read(addr+offset)._1
  def writeVal(addr: Long, v: BigInt, offset: Long = 0) : BigInt = write(addr+offset, v)
  def expectVal(addr: Long, ref: BigInt, offset: Long = 0,  msg: String = "") : Unit = {
    val v = read(addr+offset)._1
    assert(v == ref, msg)
  }

  // below are convenient functions. requires AxiModuleParams
  def softReset(c: AxiModuleParams with AxiModuleDefParams,
                maxLoopCnt: Int = 1000): Int = {
    write(c.soft_reset_rw, 1)
    var resetDone = false
    var loopCnt = 0
    while (!resetDone) {
      resetDone = read(c.soft_reset_rw)._1 > 0
      if (loopCnt > maxLoopCnt) {
        throw new RuntimeException(f"Reached maxLoopCnt:$maxLoopCnt!")
      }
      loopCnt += 1
    }
    loopCnt
  }
}
