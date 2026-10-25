# See LICENSE file for details.
# written by Kaz Yoshii <kazutomo.yoshii@gmail.com>

import os, sys, time
from pathlib import Path
import json
from types import SimpleNamespace
import logging

import pyaved

class AVED_Bridge:
    is_fpga = True

    def readAddrMap(self):
        fn = os.getenv("PARAMFN")
        if fn is None:
            raise RuntimeError("Environment variable PARAMFN is not set")

        with open(fn) as f:
            data = json.load(f, object_hook=lambda d: SimpleNamespace(**d))

        return data

    def aved_write32(self, addr, v):
        self.aved.write32(addr, v)

    def aved_read32(self, addr):
        v = self.aved.read32(addr)
        return v

    def __init__(self, base=0x1100000, dev="b1:00.0", logfn = "output.log"):
        self.base = base

        self.p = self.readAddrMap()

        self.aved = pyaved.AVED()
        self.aved.open(dev)

        log_path = Path(logfn).resolve()
        self.log = logging.getLogger("tblog")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        self.log.handlers.clear()
        handler = logging.FileHandler(log_path, mode="w")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.log.addHandler(handler)

        self.log.info("FPGA Simulation Environment Initialized!\n")

    def writelog(self, txt):
        print(txt, end='')
        with open(self.outputfn, "a")  as f:
            f.write(txt)

    def checkOffset(self, offset, align=8):
        assert (offset % align) == 0, f"offset should be aligned with {align} bytes : {offset}"

    def writeWord(self, offset, data):
        self.checkOffset(offset, align=4)
        self.aved_write32(self.base + offset, data)

    def readWord(self, offset):
        self.checkOffset(offset, align=4)
        return self.aved_read32(self.base + offset)

    def expectWord(self, offset, ref, msg = ""):
        v = self.readWord(offset)
        assert v == ref, msg

    def softReset(self, maxloopcnt = 1000):
        cycles = self.p.reset_cycles
        self.writeWord(self.p.soft_reset_rw, 1)
        reset_done = 0
        loopcnt = 0
        while reset_done == 0:
            reset_done = self.readWord(self.p.soft_reset_rw)
            if loopcnt > maxloopcnt:
                raise RuntimeError("Reached maxloopcnt. Something wrong")
            loopcnt += 1
        return loopcnt

    def setup(self):
        pass
