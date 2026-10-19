// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package garageworks

import java.io.PrintWriter

case class AddrMapEntry(name: String, addr: Int)

trait AxiAddrMapBase {
  // a quick sanity check 4-byte aligned
  def checkaddr(a : Seq[AddrMapEntry]) : Unit = {
    a.foreach { e =>
      if( (e.addr%4) > 0 ) {
        throw new RuntimeException(f"Error: not aligned with 4-byte. ${e.name} 0x${e.addr}%x")
      }
    }
    val addrs = a.map(_.addr)
    val duplicates = addrs.diff(addrs.distinct)
    if (duplicates.nonEmpty) {
      throw new RuntimeException(f"Error: duplicate addresses: ${duplicates.map(a => f"0x$a%x").mkString(", ")}")
    }
  }

  def addrMapEntries : Seq[AddrMapEntry]

  lazy val axiaddrmap: Map[String, Int] =
    addrMapEntries.map(r => r.name -> r.addr).toMap

  def writeAddrFile(filename: String, constmap: Option[Map[String,Long]] = None): Unit = {
    val out = new PrintWriter(filename)
    try {
      addrMapEntries.foreach { r =>
        out.println(f"${r.name}%-20s 0x${r.addr}%08x")
      }
      constmap match {
        case Some(cm) => cm.foreach { e =>
          out.println(f"${e._1}%-20s 0x${e._2}%08x")
        }
        case None =>
      }
    } finally {
      out.close()
    }
  }

  def githash() : Long = {
      import scala.sys.process._
    val githashstr = "git rev-parse HEAD".!!.trim
    val first8 = githashstr.take(8)
    val githash: Long = java.lang.Long.parseUnsignedLong(first8, 16)
    githash
  }
}

object Main {
  object TestAMDef extends AxiAddrMapBase {
    // definition to export
    val addrMapEntries = Seq(
      AddrMapEntry("CONST1_read_addr", 0x0),
      AddrMapEntry("CONST2_read_addr", 0x4),
    )
    checkaddr(addrMapEntries)

    val RESET_CYCLES = 8   // internal definition
  }

  def main(args: Array[String]): Unit = {
    println(s"githash=${TestAMDef.githash()}")

    val key = "CONST2_read_addr"
    val const1 = TestAMDef.axiaddrmap(key)
    println(f"${key} = 0x${const1}%08x")

    val fn = "test_axi_def.txt"
    TestAMDef.writeAddrFile(fn)
    println(s"Generated ${fn}")
  }
}
