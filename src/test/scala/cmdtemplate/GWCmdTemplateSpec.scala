// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package cmdtemplate

import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.flatspec.AnyFlatSpec
import garageworks._
import garageworks.AxiModuleParamsHelper._

class GWCmdTemplateSpec extends AnyFlatSpec with ChiselSim {
  val const1val = 0xbeefcafeL
  val const2val = getGitHash
  val p = CmdModuleParams.default(const1val, const2val)

  "test GWCmdTemplate" should "pass" in {
    simulate(new GWCmdTemplate(p, debugprint = true)) { dut =>
      val bfm = new Axi4Lite32BFM[GWCmdTemplate](dut)
      bfm.initMaster()

      bfm.expectVal(p.const1_r, const1val)
      bfm.expectVal(p.const2_r, const2val)

      val testVal = 1234
      bfm.writeVal(p.dut_rw, testVal)
      bfm.expectVal(p.dut_rw, testVal)

      // testing soft reset (e.g. initializes reg val)
      bfm.softReset(p)
      bfm.expectVal(p.dut_rw, 0)

      println("GWCmdTemplate test passed!!")
    }
  }
}
