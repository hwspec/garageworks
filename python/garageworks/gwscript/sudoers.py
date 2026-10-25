import getpass
import subprocess

from .config import AMI_TOOL, SUDO_COMMANDS, TEE


def sudoers_line():
    user = getpass.getuser()
    cmds = ", ".join([AMI_TOOL, f"{TEE} /sys/bus/pci/rescan"])
    return f"{user} ALL=(root) NOPASSWD: {cmds}"


def _nopasswd_paths():
    """
    Command paths the user may run with NOPASSWD, from `sudo -n -l`.
    'ALL' means every command.  Empty if sudo can't list without a password.
    """
    r = subprocess.run(["sudo", "-n", "-l"], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return set()

    paths = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("(") or ")" not in line:
            continue                     # only rule lines: "(root) NOPASSWD: ..."
        nopasswd = False
        for item in line.split(")", 1)[1].split(","):
            item = item.strip()
            # tags carry over to the following commands until changed
            while ":" in item and item.split(":", 1)[0].isupper():
                tag, item = item.split(":", 1)
                item = item.strip()
                if tag == "NOPASSWD":
                    nopasswd = True
                elif tag == "PASSWD":
                    nopasswd = False
            if nopasswd and item:
                paths.add(item.split()[0])
    return paths


def _missing_nopasswd():
    """
    SUDO_COMMANDS that sudo would not run without a password.

    `sudo -n -l <cmd>` alone isn't enough: it succeeds whenever the command is
    *allowed*, even if running it would still ask for a password.  So the
    command must also appear under a NOPASSWD tag.
    """
    nopasswd = _nopasswd_paths()
    missing = []
    for cmd in SUDO_COMMANDS:
        allowed = subprocess.run(["sudo", "-n", "-l", *cmd],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).returncode == 0
        if not (allowed and ("ALL" in nopasswd or cmd[0] in nopasswd)):
            missing.append(" ".join(cmd))
    return missing


def _check_nopasswd_sudo():
    """Fail fast (instead of hanging) if sudo would need a password."""
    missing = _missing_nopasswd()

    if missing:
        raise RuntimeError(
            "passwordless sudo is required for non-interactive runs but is not "
            "configured for:\n  " + "\n  ".join(missing) + "\n"
            "Add with `sudo visudo -f /etc/sudoers.d/gwscript`:\n  "
            + sudoers_line()
        )
