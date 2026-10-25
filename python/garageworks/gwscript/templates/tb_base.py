import cocotb

from target_bridge import TargetBridge


@cocotb.test()
async def tb_base(cocotb_dut):
    dut = TargetBridge(cocotb_dut)
    await dut.setup()
    await dut.softReset()

    dut.log.info("Verified!")
