# Tutorials

How to test your own design with GarageWorks. This is a minimal starting
point; worked examples (in this repo) and more text will be added later.

See the [SRV32I](https://github.com/hwspec/SRV32I) example project for a
complete, working GarageWorks setup.

## Starting from a template

These files can be used as templates for your own design:

- `src/main/scala/cmdtemplate/GWCmdTemplate.scala`: a Chisel module example
  for bridging with your DUT. It includes soft reset logic.
- `src/test/scala/cmdtemplate/GWCmdTemplateSpec.scala`: a Chisel testbench
  for `GWCmdTemplate`.
- `gwtests/{tb_cmd.py, cmd.mk, Makefile}`: a cocotb testbench for
  `GWCmdTemplate`. This testbench can be converted to an FPGA testbench on
  AMD V80 AVED (modified version) without modification. `Makefile` is shared
  and repo-agnostic; `cmd.mk` holds the repo-specific settings (module name,
  top name, generator command, ...), so the same `Makefile` can be reused by
  other repositories (e.g., SRV32I) by dropping in a different `*.mk` file.
