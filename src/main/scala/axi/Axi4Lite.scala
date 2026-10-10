// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package axi

import chisel3._
import upickle.default._

object AxiLiteResp {
  val OKAY   = 0b00
  val EXOKAY = 0b01 // unused in AXI4-lite
  val SLVERR = 0b10 // slave generated error
  val DECERR = 0b11 // address decode error
}

// A simple AXI4-Lite slave
class AxiLite(AddrBW: Int = 32, DataBW: Int = 32) extends Bundle {
  // write address
  val awaddr  = Input(UInt(AddrBW.W))
  val awvalid = Input(Bool())
  val awready = Output(Bool())

  // write data
  val wdata   = Input(UInt(DataBW.W))
  val wstrb   = Input(UInt((DataBW/8).W))
  val wvalid  = Input(Bool())
  val wready  = Output(Bool())

  // write response
  val bresp   = Output(UInt(2.W))
  val bvalid  = Output(Bool())
  val bready  = Input(Bool())

  // read addr
  val araddr  = Input(UInt(AddrBW.W))
  val arvalid = Input(Bool())
  val arready = Output(Bool())

  // read data
  val rdata   = Output(UInt(DataBW.W))
  val rresp   = Output(UInt(2.W))
  val rvalid  = Output(Bool())
  val rready  = Input(Bool())
}

class AxiLite32(AddrBW: Int = 32) extends AxiLite(AddrBW, 32)

class AxiLite32IO(addrW: Int = 32) extends Bundle {
  val AXI = new AxiLite32(addrW)
}

trait HasAxiLite32IO { this: Module =>
  val S: AxiLite32IO
}

trait AxiModuleParams {
  def moduleName: String
}

trait AxiModuleDefParams {
  def soft_reset_rw : Long
}

object AxiModuleParamsHelper {
  def writeToJson[T <: AxiModuleParams: Writer](
                                              params: T,
                                              directory: os.Path
                                            ): Unit = {
    os.write.over(
      // directory / s"${params.moduleName}_params.json",
      directory / s"params.json",
      write(params, indent = 2),
      createFolders = true
    )
  }

  private def updateFromJson[T: ReadWriter](
                                   current: T,
                                   path: os.Path
                                 ): T = {
    val currentJson = writeJs(current).obj
    val patchJson   = ujson.read(os.read(path)).obj

    patchJson.foreach {
      case (name, value) => currentJson(name) = value
    }
    read[T](currentJson)
  }

  def checkParamEnv[T: ReadWriter](current: T, envName: String) : T = {
    val fn : Option[String] = sys.env.get(envName)
    val updated = fn match {
      case Some(fn) => AxiModuleParamsHelper.updateFromJson(current, os.Path(fn))
      case None => current
    }
    updated
  }

  def getGitHash : Long = {
    import scala.sys.process._
    val gitHashStr = "git rev-parse HEAD".!!.trim
    val first8 = gitHashStr.take(8)
    val gitHash: Long = java.lang.Long.parseUnsignedLong(first8, 16)
    gitHash
  }
}
