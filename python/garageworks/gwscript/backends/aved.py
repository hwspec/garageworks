import os
import re
import shutil
import subprocess
import sys
import time

from ..config import GENERATED, ROOT, TB_DIR, TESTS
from ..shell import run
from .base import Backend


class AvedBackend(Backend):
    """AMD Alveo V80 + AVED reference design (see self.repo)."""

    name = "aved"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.repo = self.get("repo", "https://github.com/hwspec/AVED-gw.git")
        self.bdf = self.get("bdf", "b1:00.0")
        self.ami_tool = self.get("ami_tool", "/usr/local/bin/ami_tool")
        self.tee = shutil.which("tee") or "/usr/bin/tee"

    @property
    def clone_dir(self):
        return ROOT / "AVED-gw"

    @property
    def hw_dir(self):
        return self.clone_dir / "hw/amd_v80_gen5x8_25.1"

    @property
    def rtl_dir(self):
        # AVED-gw expects the top module to be named `wrapper`
        return self.hw_dir / "src/rtl/garageworks"

    @property
    def lock_key(self):
        return self.bdf

    def sudo_commands(self):
        return [
            [self.ami_tool, "reload", "-t", "sbr", "-d", self.bdf],
            [self.tee, "/sys/bus/pci/rescan"],
        ]

    def clean_paths(self):
        return ([], [self.clone_dir])

    # ------------------------------------------------------------------

    def fetch(self):
        aved = self.clone_dir

        if aved.exists():
            if not (aved / ".git").is_dir():
                raise RuntimeError(
                    f"{aved} exists but does not appear to be a git repository"
                )
            print(f"AVED repository already exists: {aved}")
        else:
            run(["git", "clone", self.repo, str(aved)])

            if not (aved / ".git").is_dir():
                raise RuntimeError("AVED clone did not complete successfully")

        os.environ["AVED"] = str(aved)
        print(f"AVED={aved}")

        # builds libami.so + libvamp.so/pyaved.py (sw/vamp/build)
        run(["make", "sw"], cwd=aved)

        return {"aved_dir": str(aved)}

    def _paramfn(self):
        """params.json if present, else the single generated *.json."""
        p = self.rtl_dir / "params.json"
        if p.is_file():
            return p
        jsons = sorted(self.rtl_dir.glob("*.json"))
        if len(jsons) != 1:
            raise RuntimeError(f"Cannot pick params JSON in {self.rtl_dir}: {jsons}")
        return jsons[0]

    def _copy_useracc(self):
        """Copy generated user-accelerator RTL/JSON into AVED-gw."""
        rtl_dir = self.rtl_dir
        rtl_dir.mkdir(parents=True, exist_ok=True)

        files = []
        for pattern in ("*.v", "*.sv", "*.json"):
            files.extend(GENERATED.glob(pattern))

        if not files:
            raise RuntimeError(f"No generated RTL/JSON files found in {GENERATED}")

        # remove previous design so stale RTL/JSON never leaks into the build
        for pattern in ("*.v", "*.sv", "*.json"):
            for old in rtl_dir.glob(pattern):
                old.unlink()

        copied = []

        for src in files:
            dst = rtl_dir / src.name
            print(f"copy {src} -> {dst}")
            shutil.copy2(src, dst)
            copied.append(str(dst))

        # GarageWorks emits user_accel_bd_wrapper.v; AVED-gw's BD references `wrapper`
        gw_wrapper = rtl_dir / "user_accel_bd_wrapper.v"
        wrapper = rtl_dir / "wrapper.v"
        if gw_wrapper.is_file():
            text = gw_wrapper.read_text()
            text, n = re.subn(r"^module\s+user_accel_bd_wrapper\b", "module wrapper",
                              text, count=1, flags=re.MULTILINE)
            if n != 1:
                raise RuntimeError(f"Could not rename module in {gw_wrapper}")
            wrapper.write_text(text)
            gw_wrapper.unlink()
            copied = [c for c in copied if c != str(gw_wrapper)] + [str(wrapper)]
            print(f"rename {gw_wrapper.name} -> {wrapper.name} (module wrapper)")

        if not wrapper.is_file():
            raise RuntimeError(f"Missing {wrapper} (top module must be `wrapper`)")

        return {"copied": copied, "paramfn": str(self._paramfn())}

    def build(self):
        useracc = self._copy_useracc()

        log_path = ROOT / "aved-compile.log"
        build_dir = self.hw_dir / "build"

        if build_dir.exists():
            print(f"Removing previous hardware build directory: {build_dir}")
            shutil.rmtree(build_dir)

        print(f"\n[{self.hw_dir}] $ ./build_all.sh")
        print(f"Logging output to {log_path}")

        with log_path.open("w") as logfile:
            proc = subprocess.Popen(
                ["./build_all.sh"],
                cwd=self.hw_dir,
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            assert proc.stdout is not None

            try:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    logfile.write(line)
            except BaseException:
                proc.terminate()            # don't leave Vivado running
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise

            rc = proc.wait()

        if rc != 0:
            raise subprocess.CalledProcessError(rc, ["./build_all.sh"])

        print("Hardware build completed successfully.")
        return {**useracc, "log": str(log_path)}

    def _device_present(self):
        r = subprocess.run(["lspci", "-s", self.bdf], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return bool(r.stdout.strip())

    def _pci_rescan(self):
        run(["sudo", self.tee, "/sys/bus/pci/rescan"], input="1\n")
        time.sleep(2)

    def program(self):
        prog_result = run(["make", "prog"], cwd=self.clone_dir)

        # JTAG programming drops the PCIe link; the kernel may remove the device
        if not self._device_present():
            self._pci_rescan()

        if self._device_present():
            # sbr removes the device itself and rescans; re-check afterwards.
            # Must succeed: without the reload the new bitstream isn't usable,
            # even though the device may still show up on PCIe.
            run(["sudo", self.ami_tool, "reload", "-t", "sbr", "-d", self.bdf])
            time.sleep(2)

        if not self._device_present():
            self._pci_rescan()

        if not self._device_present():
            raise RuntimeError(
                f"{self.bdf} not on PCIe after programming; power cycle may be needed"
            )

        run([self.ami_tool, "overview"], ignore_returncode=True)

        return {
            "make_prog_returncode": prog_result.returncode,
            "bdf": self.bdf,
        }

    def test(self, testname=None):
        aved = self.clone_dir
        env = os.environ.copy()
        env["AVED"] = str(aved)

        vamp_build = aved / "sw/vamp/build"   # libvamp.so + pyaved.py
        ami_build = aved / "sw/AMI/api/build"  # libami.so

        old_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{old_pythonpath}:{vamp_build}" if old_pythonpath else str(vamp_build)
        )

        old_ldpath = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{vamp_build}:{ami_build}" + (
            f":{old_ldpath}" if old_ldpath else ""
        )

        env["PARAMFN"] = str(self._paramfn())
        env["GW_BRIDGE_NAME"] = self.bridge_name

        targets = [testname] if testname else TESTS
        if not targets:
            raise RuntimeError("No tests defined in .gwconfig (example: tests=basic,loop)")

        results = []

        for target in targets:
            print(f"\nRunning FPGA test target: {target}")
            run(
                ["make", "-f", "Makefile.gw", f"hw_{target}"],
                cwd=TB_DIR,
                env=env,
            )

            output_log = TB_DIR / "output.log"
            if not output_log.is_file():
                raise RuntimeError(f"Expected output file not found: {output_log}")

            print(f"\nFPGA output ({target}): {output_log}")
            print("-" * 72)

            lines = output_log.read_text(errors="replace").splitlines()
            for line in lines[-30:]:
                print(line)

            results.append({
                "testname": target,
                "output_log": str(output_log),
            })

        return {"tests": results}
