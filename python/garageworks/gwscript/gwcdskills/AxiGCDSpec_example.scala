// See README.md for license details.
package axigcd

import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.flatspec.AnyFlatSpec
import garageworks._
import garageworks.AxiModuleParamsHelper._

class AxiGCDSpec extends AnyFlatSpec with ChiselSim {
  val const1val = 0xa415c0dL
  val const2val = getGitHash
  val p = AxiGcdParams.default(const1val, const2val)

  // reference model
  def gcd(a: BigInt, b: BigInt): BigInt = if (b == 0) a else gcd(b, a % b)

  // drives value1/value2 over AXI-Lite, waits for valid_r, and checks result_r
  // against the software-computed gcd.
  def runGcdTest(bfm: Axi4Lite32BFM[AxiGCD], value1: Long, value2: Long): Unit = {
    bfm.writeVal(p.value1_rw, value1)
    bfm.writeVal(p.value2_rw, value2) // kicks off the computation

    var valid: BigInt = 0
    var tries = 0
    while (valid == 0 && tries < 1000) {
      valid = bfm.readVal(p.valid_r)
      tries += 1
    }
    assert(valid == 1, s"AxiGCD did not finish in time for ($value1, $value2)")
    bfm.expectVal(p.result_r, gcd(value1, value2), msg = s"gcd($value1, $value2)")

    val cycles = bfm.readVal(p.gcd_cycles_r)
    println(s"gcd($value1, $value2) = ${gcd(value1, value2)}, took $cycles cycles")
  }

  val testVectors = Seq(
    (48L, 18L),
    (18L, 48L),
    (1071L, 462L),
    (17L, 5L),
    (0L, 42L),
    (42L, 0L),
    (7L, 7L),
    (0L, 0L),
  )

  "test AxiGCD" should "pass" in {
    simulate(new AxiGCD(p, debugprint = true)) { dut =>
      val bfm = new Axi4Lite32BFM[AxiGCD](dut)
      bfm.initMaster()

      bfm.expectVal(p.const1_r, const1val)
      bfm.expectVal(p.const2_r, const2val)

      for ((value1, value2) <- testVectors) {
        runGcdTest(bfm, value1, value2)
      }

      // testing soft reset (e.g. initializes reg val)
      bfm.softReset(p)
      bfm.expectVal(p.valid_r, 0)

      println("AxiGCD test passed!!")
    }
  }
}
