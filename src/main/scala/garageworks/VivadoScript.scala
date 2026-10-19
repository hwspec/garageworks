// Copyright (c) 2026, UChicago Argonne, LLC
// All Rights Reserved
// SPDX-License-Identifier: BSD-3-Clause
// See LICENSE file for details.
package garageworks

import java.io.PrintWriter
import java.nio.file.attribute.PosixFilePermissions
import java.nio.file.{Files, Paths}

object VivadoScript {
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


  def generate(flist: List[String], topname: String, dir: String, model: String): Unit = {
    println(f"vivado script generation: model=$model")

    val clkp = 10.0 // in ns

    val constraints =
      f"""
         |create_clock -name clock -period $clkp [get_ports clock]
         |""".stripMargin

    writeto(dir + "/constraints.xdc", constraints)

    // XXX: part_name is hardcoded now
    val runtcl1 =
      f"""
         |set project_name $topname
         |set part_name xcv80-lsva4737-2MHP-e-S
         |
         |create_project $$project_name ./ -part $$part_name
         |
         |""".stripMargin


    val runtcl2 : String =
      flist.map(f => s"add_files -norecurse $f").mkString("\n") + "\n\n"

    val phys_opt_design = "phys_opt_design -interconnect_retime -lut_opt"
    // phys_opt_design -slr_crossing_opt -tns_cleanup  # for U280

    val runtcl3 =
      f"""
         |set_property top $topname [current_fileset]
         |add_files -fileset constrs_1 ./constraints.xdc
         |
         |launch_runs synth_1 -jobs 8
         |wait_on_run synth_1
         |
         |launch_runs impl_1 -jobs 8
         |wait_on_run impl_1
         |
         |open_run impl_1
         |
         |$phys_opt_design
         |route_design
         |
         |report_utilization -file utilization_report.txt
         |
         |report_timing_summary -file timing_summary.txt
         |report_timing -delay_type max -max_paths 2 -sort_by group -file critical_path_timing.txt
         |
         |write_checkpoint -force implemented.dcp
         |exit
         |""".stripMargin




    val runtcl = runtcl1 + runtcl2 + runtcl3

    writeto(dir + "/run.tcl", runtcl)

    val runsh =
      f"""
         |time vivado -mode batch -source run.tcl
         |""".stripMargin

    writeto(dir + "/compile.sh", runsh, executable = true)
  }
}

