// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package axi_examples

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.flatspec.AnyFlatSpec
import axi._
import axi.AxiModuleParamsHelper._
import axi_examples.TestQModuleParams._
import scala.util.Random

class Axi4Lite32TestQSpec extends AnyFlatSpec with ChiselSim {
  val const1 = 0xbeefcafeL
  val const2 = getGitHash
  val npxs = 8
  val nrows = 4
  val pxbw = 12
  val inqsize : Int = 1024
  val outqsize : Int = 16
  val threshold = 20
  val axibw = 32

  def mkmask(p1: Int, p2: Int) : BigInt = {
    val width = p2 - p1 + 1
    (BigInt(1) << width) - 1
  }
  def updateBits(x: BigInt, pos1: Int, pos2: Int, value: BigInt): BigInt = {
    require(pos1 >= 0 && pos2 >= pos1)
    val fieldMask = mkmask(pos1, pos2)
    val mask = fieldMask << pos1                  // positioned mask
    val cleared = x & ~mask                       // clear target bits
    val inserted = (value & fieldMask) << pos1    // truncate and position value
    cleared | inserted
  }
  def updateField(currow : BigInt, pos : Int, v : Int, bw : Int) : BigInt = {
    val bpos1 = pos * bw
    val bpos2 = bpos1 + bw - 1
    updateBits(currow, bpos1, bpos2, v)
  }
  def readField(currow : BigInt, pos : Int, bw : Int) : BigInt = {
    val bpos1 = pos * bw
    val bpos2 = bpos1 + bw - 1
    val fieldMask = mkmask(bpos1, bpos2)
    (currow >> bpos1) & fieldMask
  }

  "test AxiList32TestQ" should "pass" in {
    val p = checkParamEnv(
      TestQModuleParams.default(const1 = const1, const2 = const2,
        npxs = npxs, nrows = nrows, pxbw = pxbw,
        inqsize = inqsize, outqsize = outqsize, threshold = threshold),
      "TESTQ_MODULE_PARAMS")

    def startFeed[D <: Module with HasAxiLite32IO](
      bfm: Axi4Lite32BFM[D]
    ) : Unit = {
      bfm.writeVal(p.start_feed_w, 1)
      assert(bfm.readVal(p.inq_cnt_r) > 0)
      var done = false
      while(!done) {
        if (bfm.readVal(p.drained_r) > 0)
          done = true
      }
    }

    def commitRow[D <: Module with HasAxiLite32IO](
       bfm: Axi4Lite32BFM[D], rowid : Int, pxs: List[Int]) : Unit = {
      var tmp : BigInt = 0
      for (e <- pxs.zipWithIndex) {
        tmp = updateField(tmp, pos=e._2, v=e._1, bw=pxbw)
      }
      val nwords = ((npxs*pxbw) + axibw - 1)/axibw
      for (i <- 0 until nwords) {
        val v = readField(tmp, i, axibw)
        bfm.writeVal(p.fillup_w, v, offset = i*4)
      }
      bfm.writeVal(p.commit_w, rowid)
    }


    simulate(new Axi4Lite32TestQ(p, debugprint = true)) { dut =>
      val bfm = new Axi4Lite32BFM(dut)
      bfm.initMaster()

      // check the module id
      bfm.expectVal(p.const1_r, const1)
      bfm.expectVal(p.const2_r, const2)

      def checkOutput(nzcnt: Int) : Unit = {
        val inqcnt = bfm.readVal(p.inq_cnt_r)
        assert(inqcnt == nrows)
        startFeed(bfm)
        bfm.expectVal(p.outq_cnt_r, 1)
        bfm.expectVal(p.outq_r, nzcnt)
        bfm.expectVal(p.outq_cnt_r, 0)
      }

      def testFixPattern() : Unit = {
        bfm.softReset(p)
        var nzcnt = 0
        for (i <- 0 until nrows) {
          val rowpxs = List.tabulate(npxs) { j => if ((j%4) == 0 || j<=i) threshold + 2 else 0 }
          nzcnt += rowpxs.count(_ > 0)
          commitRow(bfm, i, rowpxs)
        }
        checkOutput(nzcnt)
      }

      def testRndPattern() : Unit = {
        bfm.softReset(p)
        var nzcnt = 0
        for (i <- 0 until nrows) {
          val rowpxs = List.tabulate(npxs) { j => if (Random.nextBoolean()) threshold + 2 else 0 }
          nzcnt += rowpxs.count(_ > 0)
          commitRow(bfm, i, rowpxs)
        }
        checkOutput(nzcnt)
      }

      //for(i <- 0 until 3)  testFixPattern()
      for(i <- 0 until 10)  testRndPattern()

      println("Axi4Lite32TestQ test passed!")
    }
  }
}
