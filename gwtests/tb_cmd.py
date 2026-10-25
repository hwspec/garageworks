import cocotb
from garageworks.bridge.cocotb_bridge import COCOTB_Bridge

@cocotb.test()
async def sim_cmd(cocotb_dut):
    dut = COCOTB_Bridge(cocotb_dut)
    await dut.setup()
    
    await dut.expectWord(dut.p.const1_r, dut.p.const1)
    await dut.expectWord(dut.p.const2_r, dut.p.const2)

    testval = 0x555aaa
    await dut.writeWord(dut.p.dut_rw, testval)
    v = await dut.readWord(dut.p.dut_rw)
    dut.log.info(f"dutout: {v:08x}")
    assert v == testval

    await dut.softReset()
    
    v = await dut.readWord(dut.p.dut_rw)
    dut.log.info(f"dutout: {v:08x}")
    assert v == 0

    dut.log.info("Done!!\n")
