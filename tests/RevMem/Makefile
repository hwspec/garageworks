#
# This is Makefile for cocotb
#

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
TOPNAME=wrapper
HWMODULENAME=Axi4Lite32RevMem
SRCDIR = $(abspath ../../generated/$(HWMODULENAME))
VERILOG_SOURCES += $(SRCDIR)/*.v $(SRCDIR)/*.sv
WAVES = 1
EXTRA_ARGS += --trace --trace-structs

test : $(SRCDIR)/$(HWMODULENAME).sv
	@echo $(VERILOG_SOURCES)
	@PARAMFN=$(SRCDIR)/params.json $(MAKE) sim MODULE=tb_revmem TOPLEVEL=$(TOPNAME)
	@echo -e "\n[Output]"
	@cat output.log

$(SRCDIR)/$(HWMODULENAME).sv :
	(cd ../../ ; sbt "runMain axi_examples.Axi4Lite32RevMem")

include $(shell cocotb-config --makefiles)/Makefile.sim

clean::
	rm -f output.txt results.xml *.vcd fpga_simple.py
	rm -rf __pycache__
