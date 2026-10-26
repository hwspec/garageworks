from abc import ABC, abstractmethod


class Backend(ABC):
    """
    A pluggable FPGA backend: how gwscript fetches, builds, programs and
    tests a design against one hardware target, selected via .gwconfig's
    `platform=` key (default: aved) or --platform.

    `name` also doubles as the bridge-module naming convention used by
    conv_cocotb_to_fpga (bridge/{name.lower()}_bridge.py's {bridge_name}_Bridge
    class) -- see bridge_name below.
    """

    name = None

    def __init__(self, cfg):
        """cfg is the full parsed .gwconfig dict; read '{self.name}.KEY'
        entries from it for backend-specific settings."""
        self.cfg = cfg

    def get(self, key, default=None):
        """Read a '{self.name}.{key}' entry from .gwconfig."""
        return self.cfg.get(f"{self.name}.{key}", default)

    @property
    def clone_dir(self):
        """Project-local directory this backend's board-support repo is
        cloned into. One per project; never shared across projects."""
        raise NotImplementedError

    @property
    def lock_key(self):
        """Identifies the physical board, for serializing BOARD_STAGES
        across processes/projects. Defaults to the backend name."""
        return self.name

    @property
    def bridge_name(self):
        """--fpga value passed to conv_cocotb_to_fpga; matches
        bridge/{bridge_name.lower()}_bridge.py's {bridge_name}_Bridge class.
        Defaults to the backend name, upper-cased."""
        return self.name.upper()

    @abstractmethod
    def fetch(self):
        """Clone/prepare the board-support repository."""

    @abstractmethod
    def build(self):
        """Copy the generated RTL in and build the bitstream."""

    @abstractmethod
    def program(self):
        """Load the built bitstream onto the board."""

    @abstractmethod
    def test(self, testname=None):
        """Run one (or all configured) tests against real hardware."""

    def sudo_commands(self):
        """Commands program()/test() need passwordless sudo for, as argv
        lists -- used for --sudoers and the non-interactive NOPASSWD check."""
        return []

    def clean_paths(self):
        """(files, dirs) --clean-all should remove for this backend."""
        return ([], [])
