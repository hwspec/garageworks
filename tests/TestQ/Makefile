#
# This is Makefile for cocotb
#

SIM ?= verilator
TOPLEVEL_LANG ?= verilog

TOPNAME=wrapper
HWMODULENAME=Axi4Lite32TestQ

SRCDIR = $(abspath ../../generated/$(HWMODULENAME))
VERIFDIR := $(SRCDIR)/verification

VERILOG_SOURCES += $(SRCDIR)/*.v $(SRCDIR)/*.sv

# Compile only the top-level verification layer.
VERILOG_SOURCES += $(VERIFDIR)/layers-Axi4Lite32TestQ-Verification.sv

# Needed for `include "layers-BinImageCount-Verification.sv"
VERILOG_INCLUDE_DIRS += $(VERIFDIR)

WAVES = 1
EXTRA_ARGS += --trace --trace-structs

test : $(SRCDIR)/$(HWMODULENAME).sv
	@echo $(VERILOG_SOURCES)
	@PARAMFN=$(SRCDIR)/params.json $(MAKE) sim MODULE=tb_testq TOPLEVEL=$(TOPNAME)
	@echo -e "\n[Output]"
	@cat output.log

$(SRCDIR)/$(HWMODULENAME).sv :
	(cd ../../ ; sbt "runMain axi_examples.$(HWMODULENAME)")

include $(shell cocotb-config --makefiles)/Makefile.sim

clean::
	rm -f output.txt results.xml *.vcd fpga_tb_testq.py
	rm -rf __pycache__
