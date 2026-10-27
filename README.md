# GarageWorks

> **Status: beta release.** APIs, directory layout, and build flow may change.

GarageWorks is a lightweight FPGA testing framework that lets the same cocotb
testbench run on both software RTL simulation and FPGA hardware. See
[docs/garageworks.md](docs/garageworks.md) for more, and [docs/](docs/) for
tutorials and development notes.

## Setup

### 1. Python environment

Use conda or a venv; either works.

conda:
```
conda create -n garage python=3.11 pip
conda activate garage
```

venv:
```
python3 -m venv .venv
source .venv/bin/activate
```

Then install GarageWorks (see [docs/garageworks.md](docs/garageworks.md) for
why the version is left unpinned):
```
pip install garageworks
```

To work on GarageWorks itself instead, clone this repo and install it in
editable mode: `pip install -e python/`.

### 2. Other tools

- **SBT** for Chisel RTL generation (pinned version in `project/build.properties`)
- **Verilator** for cocotb simulation (`sh misc/build_verilator.sh INSTDIR`,
  then add `INSTDIR/bin` to `PATH`)
- FPGA tests additionally need **Vivado/Vitis** and an AMD Alveo V80 + AVED
  board; see [docs/development-notes.md](docs/development-notes.md) for
  versions and setup.

## Try it: the SRV32I example

[SRV32I](https://github.com/hwspec/SRV32I) is a small RISC-V core that
demonstrates the full GarageWorks workflow, from RTL generation and cocotb
simulation through FPGA testing.

```
git clone https://github.com/hwspec/SRV32I
cd SRV32I
```

Run the cocotb simulation test:
```
gwscript
```

Run the FPGA test (after sourcing your Vitis/Vivado environment):
```
gwscript --fpga
```

See `gwscript --help` and [SRV32I's README](https://github.com/hwspec/SRV32I#readme)
for more (other test targets, FPGA env variables, MCP mode).

## License

BSD. See [LICENSE](LICENSE).

## Contact

For questions, bug reports, or feature requests, please open an issue on
GitHub. For general discussion, please use GitHub Discussions.
