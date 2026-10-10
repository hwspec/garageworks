// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package axi

import _root_.circt.stage.ChiselStage
import chisel3._

import java.io.PrintWriter
import java.nio.file.attribute.PosixFilePermissions
import java.nio.file.{Files, Paths}
import scala.io.Source
import scala.reflect.ClassTag
import scala.util.Using
import upickle.default._

object EmitVerilog {
  def readfilelist(filelistfn: String) : List[String] = {
    val p = Paths.get(filelistfn)
    if (Files.isRegularFile(p)) {
      val lines: List[String] =
        Using.resource(Source.fromFile(filelistfn)) { src =>
          src.getLines().toList
        }
      lines
    } else
      List()
  }

//  def apply(gen: => RawModule) : Unit = {
//    generate(gen)
//  }

  def generate[T <: RawModule : ClassTag, P <: AxiModuleParams: Writer](gen: => T,
                                          params: P,
                                          firtoolOpts : Array[String] = Array(
                                            "--disable-all-randomization",
                                            "--strip-debug-info",
                                            "--lowering-options=disallowLocalVariables,disallowPackedArrays",
                                            "--verilog",
                                          ),
                                          opts : Map[String, String] = Map("vivado" -> "v80", "axiwrapper" -> "bd"),
                                          addrmap : Option[AxiAddrMapBase] = None,
                                          constmap : Option[Map[String, Long]] = None
                                         ) : String = {
    val topname = implicitly[ClassTag[T]].runtimeClass.getSimpleName
    val targetdir = "generated/" + topname
    Files.createDirectories(java.nio.file.Paths.get(targetdir))

    val args_a = Array("--target-dir", targetdir)

    val st = System.nanoTime()
    ChiselStage.emitSystemVerilogFile(gen,
      args = args_a,
      firtoolOpts = firtoolOpts)
    val et = (System.nanoTime() - st)
    val ets = et.toDouble * 1e-9
    println(f"Verilog generation: ${ets}%.2f sec")

    if (opts.contains("axiwrapper")) {
      genAxiWrapper(targetdir, topname, "wrapper")
    }
    if (opts.contains("vivado")) {
      val flist = readfilelist(targetdir + "/filelist.f")
      VivadoScript.generate(flist,  topname, targetdir, opts("vivado"))
    }

    AxiModuleParamsHelper.writeToJson(params, os.Path(targetdir, os.pwd))

    genCopyScript(targetdir)

    // remove below
    addrmap match {
      case Some(am) => am.writeAddrFile(targetdir + "/addrmap.txt", constmap)
      case None =>
    }

    os.Path(targetdir, os.pwd).toString
  }

  def writeto(fn: String, text: String, executable: Boolean = false) : Unit = {
    new PrintWriter(fn) {
      write(text)
      close()
    }
    if (executable) {
      val path = Paths.get(fn)
      Files.setPosixFilePermissions(path, PosixFilePermissions.fromString("rwxrw-r--"))
    }
  }

  private def genCopyScript(targetDir: String) : Unit = {
    val str =
      s"""
         |if [ -z "$$1" ] ; then
         |	echo "usage: $$1 destdir"
         |	exit 1
         |fi
         |
         |targetdir=$$1
         |scp *.v *.sv *.json $$targetdir
         |
         |""".stripMargin

    val fn = targetDir + "/" + "copysrcto.sh"
    writeto(fn, str, executable = true)
  }

  private def genAxiWrapper(targetDir : String, topName : String,
                            axiWrapperName : String = "") : Unit = {
    val axitop = if (axiWrapperName.isEmpty) {
      topName + "_bd_wrapper"
    } else axiWrapperName
    val fn = targetDir + "/" + axitop + ".v"

    val str =
      s"""
        |module $axitop (
        |  input  wire        s_axi_aclk,
        |  input  wire        s_axi_aresetn,
        |
        |  input  wire [31:0] S_AXI_awaddr,
        |  input  wire        S_AXI_awvalid,
        |  output wire        S_AXI_awready,
        |  input  wire [31:0] S_AXI_wdata,
        |  input  wire [ 3:0] S_AXI_wstrb,
        |  input  wire        S_AXI_wvalid,
        |  output wire        S_AXI_wready,
        |  output wire [ 1:0] S_AXI_bresp,
        |  output wire        S_AXI_bvalid,
        |  input  wire        S_AXI_bready,
        |  input  wire [31:0] S_AXI_araddr,
        |  input  wire        S_AXI_arvalid,
        |  output wire        S_AXI_arready,
        |  output wire [31:0] S_AXI_rdata,
        |  output wire [ 1:0] S_AXI_rresp,
        |  output wire        S_AXI_rvalid,
        |  input  wire        S_AXI_rready
        |);
        |
        |  wire s_axi_reset = ~s_axi_aresetn;
        |
        |  $topName u_core (
        |    .clock           (s_axi_aclk),
        |    .reset           (s_axi_reset),
        |    .S_AXI_awaddr    (S_AXI_awaddr),
        |    .S_AXI_awvalid   (S_AXI_awvalid),
        |    .S_AXI_awready   (S_AXI_awready),
        |    .S_AXI_wdata     (S_AXI_wdata),
        |    .S_AXI_wstrb     (S_AXI_wstrb),
        |    .S_AXI_wvalid    (S_AXI_wvalid),
        |    .S_AXI_wready    (S_AXI_wready),
        |    .S_AXI_bresp     (S_AXI_bresp),
        |    .S_AXI_bvalid    (S_AXI_bvalid),
        |    .S_AXI_bready    (S_AXI_bready),
        |    .S_AXI_araddr    (S_AXI_araddr),
        |    .S_AXI_arvalid   (S_AXI_arvalid),
        |    .S_AXI_arready   (S_AXI_arready),
        |    .S_AXI_rdata     (S_AXI_rdata),
        |    .S_AXI_rresp     (S_AXI_rresp),
        |    .S_AXI_rvalid    (S_AXI_rvalid),
        |    .S_AXI_rready    (S_AXI_rready)
        |  );
        |endmodule
        |""".stripMargin

    writeto(fn, str)
  }
}
