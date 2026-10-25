# See LICENSE file for details.
# written by Kaz Yoshii <kazutomo.yoshii@gmail.com>

import os, sys, random
from pathlib import Path
import json
from types import SimpleNamespace
import logging

import cocotb_test.simulator, pytest
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer
from cocotb.regression import TestFactory
from cocotbext.axi import AxiLiteBus, AxiLiteMaster, AxiLiteRam, AxiResp
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

class COCOTB_Bridge:
    is_fpga = False

    def readParams(self):
        fn = os.getenv("PARAMFN")
        if fn is None:
            raise RuntimeError("Environment variable PARAMFN is not set")

        with open(fn) as f:
            data = json.load(f, object_hook=lambda d: SimpleNamespace(**d))

        return data

    async def setup(self):
        cocotb.start_soon(Clock(self.dut.s_axi_aclk, 10, units="ns").start())

        bus = AxiLiteBus.from_prefix(self.dut, "S_AXI")
        self.axi_master = AxiLiteMaster(
            bus,
            self.dut.s_axi_aclk,        # lower-case clock
            self.dut.s_axi_aresetn,     # lower-case reset
            reset_active_level=0   # because aresetn is active-low
        )
        await self.reset_dut()

    def __init__(self, dut, logfn = "output.log"):
        self.dut = dut

        self.p = self.readParams()

        log_path = Path(logfn).resolve()
        self.log = logging.getLogger("tblog")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.log.handlers.clear()
        handler = logging.FileHandler(log_path, mode="w")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.log.addHandler(handler)

        self.log.info("Cocotb Test Initialized!\n")

    async def writeWord(self, addr, data):
        b = bytearray(data.to_bytes(4, byteorder="little"))
        await self.axi_master.write(addr, b)

    async def readWord(self, addr):
        b = await self.axi_master.read(addr, 4)
        v = int.from_bytes(b, byteorder="little")
        return v

    async def expectWord(self, addr, ref, msg = ""):
        v = await self.readWord(addr)
        assert v == ref, msg

    async def softReset(self, maxloopcnt = 1000):
        cycles = self.p.reset_cycles
        await self.writeWord(self.p.soft_reset_rw, 1)
        reset_done = 0
        loopcnt = 0
        while reset_done == 0:
            reset_done = await self.readWord(self.p.soft_reset_rw)
            if loopcnt > maxloopcnt:
                raise RuntimeError("Reached maxloopcnt. Something wrong")
            loopcnt += 1
        return loopcnt

    # obsolute below
    async def reset_dut(self, cycles=5):
        self.dut.s_axi_aresetn.value = 0  # assert (active-low)
        for _ in range(cycles):
            await RisingEdge(self.dut.s_axi_aclk)
            self.dut.s_axi_aresetn.value = 1  # deassert
        for _ in range(2):
            await RisingEdge(self.dut.s_axi_aclk)
