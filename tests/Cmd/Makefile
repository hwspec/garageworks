#
# This is Makefile for cocotb
#

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
TOPNAME=wrapper
HWMODULENAME=Axi4Lite32Cmd
SRCDIR = $(abspath ../../generated/$(HWMODULENAME))
VERILOG_SOURCES += $(SRCDIR)/$(TOPNAME).v $(SRCDIR)/*.sv
VERILOG_SOURCES += $(SRCDIR)/verification/*.sv
VERILOG_SOURCES += $(abspath ../../verilog/Cmd/)/*.v # user verilog module

WAVES = 1
EXTRA_ARGS += --trace --trace-structs

test : $(SRCDIR)/$(HWMODULENAME).sv
	@echo $(VERILOG_SOURCES)
	@PARAMFN=$(SRCDIR)/params.json $(MAKE) sim MODULE=tb_cmd TOPLEVEL=$(TOPNAME)
	@echo -e "\n[Output]"
	@cat output.log

$(SRCDIR)/$(HWMODULENAME).sv gen:
	(cd ../../ ; sbt "runMain axi_examples.$(HWMODULENAME)")

include $(shell cocotb-config --makefiles)/Makefile.sim

clean::
	rm -f output.txt results.xml *.vcd fpga_simple.py
	rm -rf __pycache__
