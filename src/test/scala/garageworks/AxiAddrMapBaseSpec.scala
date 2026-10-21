// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package garageworks

import org.scalatest.flatspec.AnyFlatSpec
import java.io.File
import scala.io.Source

class AxiAddrMapBaseSpec extends AnyFlatSpec {
  object TestAMDef extends AxiAddrMapBase {
    val addrMapEntries = Seq(
      AddrMapEntry("CONST1_read_addr", 0x0),
      AddrMapEntry("CONST2_read_addr", 0x4),
    )
    checkaddr(addrMapEntries)

    val RESET_CYCLES = 8   // internal definition
  }

  "axiaddrmap" should "map names to their addresses" in {
    assert(TestAMDef.axiaddrmap("CONST1_read_addr") == 0x0)
    assert(TestAMDef.axiaddrmap("CONST2_read_addr") == 0x4)
  }

  "checkaddr" should "reject addresses that are not 4-byte aligned" in {
    assertThrows[RuntimeException] {
      TestAMDef.checkaddr(Seq(AddrMapEntry("misaligned", 0x2)))
    }
  }

  it should "reject duplicate addresses" in {
    assertThrows[RuntimeException] {
      TestAMDef.checkaddr(Seq(
        AddrMapEntry("a", 0x0),
        AddrMapEntry("b", 0x0),
      ))
    }
  }

  "githash" should "return the current commit as a non-negative Long" in {
    assert(TestAMDef.githash() >= 0L)
  }

  "writeAddrFile" should "write addresses (and optional constants) to a file" in {
    val fn = File.createTempFile("axi_addr_map", ".txt").getAbsolutePath
    try {
      TestAMDef.writeAddrFile(fn, Some(Map("MY_CONST" -> 0x123L)))
      val lines = Source.fromFile(fn).getLines().toSeq
      assert(lines.exists(_.contains("CONST1_read_addr")))
      assert(lines.exists(_.contains("CONST2_read_addr")))
      assert(lines.exists(_.contains("MY_CONST")))
    } finally {
      new File(fn).delete()
    }
  }
}
