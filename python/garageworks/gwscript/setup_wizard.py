"""First-run scaffolding for .gwconfig, Makefile.gw and target.mk.

Invoked by config.load_config() (via a local import, to avoid a cycle: this
module is imported only lazily, config.py stays a leaf for everyone else)
when .gwconfig is missing. Two entry points:

  run_setup_wizard(root, config_file)
    Interactive flow (2 questions: chiseltop, testdir); not used when
    gwscript is non-interactive or the command is -h/--help/--version.

  run_automated_setup(root, config_file, chiseltop)
    Non-interactive flow driven by `gwscript --setup CHISELTOP`; always
    uses testdir=gwtests.

Both write the same files -- .gwconfig (chiseltop, testdir, tests=base),
Makefile.gw, target.mk, target_bridge.py (empty COCOTB_Bridge subclass)
and tb_base.py (a minimal cocotb test using it) -- and return a dict
shaped like a parsed .gwconfig, so load_config() can validate it exactly
like a hand-written file.
"""

import os
import re
from importlib.resources import files

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)
_OBJECT_RE = re.compile(r"\bobject\s+(\w+)\s+extends\s+App\b")


def _find_chiseltop_candidates(root):
    """Scan src/main/scala for `object X extends App` blocks that call
    EmitVerilog.generate(...) -- these are exactly what `sbt runMain
    package.X` (i.e. chiseltop) can target."""
    src = root / "src" / "main" / "scala"
    if not src.is_dir():
        return []

    candidates = []
    for path in sorted(src.rglob("*.scala")):
        text = path.read_text(errors="replace")
        pkg_match = _PACKAGE_RE.search(text)
        if not pkg_match:
            continue
        package = pkg_match.group(1)

        matches = list(_OBJECT_RE.finditer(text))
        for i, obj_match in enumerate(matches):
            body_start = obj_match.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if "EmitVerilog.generate" in text[body_start:body_end]:
                candidates.append(f"{package}.{obj_match.group(1)}")

    return candidates


def _ask_chiseltop(root):
    candidates = _find_chiseltop_candidates(root)

    if candidates:
        print("Found possible chiseltop targets in src/main/scala:")
        for i, candidate in enumerate(candidates, 1):
            print(f"  {i}) {candidate}")
        prompt = f"chiseltop [1-{len(candidates)}, or type package.ClassName]: "
        if len(candidates) == 1:
            prompt = f"chiseltop [{candidates[0]}]: "
    else:
        prompt = "chiseltop (package.ClassName), e.g. srv32i.AxiSRV32I: "

    while True:
        value = input(prompt).strip()

        if not value and len(candidates) == 1:
            return candidates[0]

        if value.isdigit() and candidates:
            index = int(value)
            if 1 <= index <= len(candidates):
                return candidates[index - 1]
            print(f"Enter a number between 1 and {len(candidates)}, or a package.ClassName.")
            continue

        if not value:
            print("This value is required.")
            continue
        if "." not in value:
            print("chiseltop must contain a '.' separating package and class name.")
            continue
        return value


def _ask_testdir():
    value = input("Test directory [gwtests]: ").strip()
    return value or "gwtests"


def _scaffold(root, config_file, chiseltop, testdir):
    name = chiseltop.rpartition(".")[2]
    bridge_basename = "target_bridge"

    tb_dir = (root / testdir).resolve()
    relroot = os.path.relpath(root, tb_dir)

    if config_file.exists():
        print(f"{config_file} already exists, left as is.")
    else:
        lines = [f"chiseltop={chiseltop}"]
        if testdir != ".":
            lines.append(f"testdir={testdir}")
        lines.append("tests=base")
        config_file.write_text("\n".join(lines) + "\n")
        print(f"Wrote {config_file}")

    tb_dir.mkdir(parents=True, exist_ok=True)
    templates = files("garageworks.gwscript") / "templates"

    makefile_dst = tb_dir / "Makefile.gw"
    if makefile_dst.exists():
        print(f"{makefile_dst} already exists, left as is.")
    else:
        makefile_dst.write_text((templates / "Makefile.gw").read_text())
        print(f"Wrote {makefile_dst}")

    target_mk_dst = tb_dir / "target.mk"
    if target_mk_dst.exists():
        print(f"{target_mk_dst} already exists, left as is.")
    else:
        tmpl = (templates / "target.mk.tmpl").read_text()
        target_mk_dst.write_text(tmpl.format(
            name=name,
            bridge_basename=bridge_basename,
            relroot=relroot,
            chiseltop=chiseltop,
        ))
        print(f"Wrote {target_mk_dst}")

    bridge_dst = tb_dir / f"{bridge_basename}.py"
    if bridge_dst.exists():
        print(f"{bridge_dst} already exists, left as is.")
    else:
        bridge_dst.write_text((templates / "target_bridge.py").read_text())
        print(f"Wrote {bridge_dst}")

    tb_base_dst = tb_dir / "tb_base.py"
    if tb_base_dst.exists():
        print(f"{tb_base_dst} already exists, left as is.")
    else:
        tb_base_dst.write_text((templates / "tb_base.py").read_text())
        print(f"Wrote {tb_base_dst}")

    return {"chiseltop": chiseltop, "testdir": testdir, "tests": "base"}


def run_setup_wizard(root, config_file):
    """Interactive flow: 2 questions (chiseltop, testdir)."""
    print(f"No .gwconfig found in {root}.")
    print("Setting up a new gwscript project -- 2 questions:\n")

    chiseltop = _ask_chiseltop(root)
    testdir = _ask_testdir()

    tb_dir = (root / testdir).resolve()
    print("\nAbout to create:")
    print(f"  {config_file}  (chiseltop={chiseltop}, testdir={testdir}, tests=base)")
    print(f"  {tb_dir / 'Makefile.gw'}")
    print(f"  {tb_dir / 'target.mk'}")
    print(f"  {tb_dir / 'target_bridge.py'}")
    print(f"  {tb_dir / 'tb_base.py'}")

    confirm = input("\nProceed? [Y/n]: ").strip().lower()
    if confirm not in ("", "y", "yes"):
        raise RuntimeError("Setup cancelled.")

    return _scaffold(root, config_file, chiseltop, testdir)


def run_automated_setup(root, config_file, chiseltop):
    """Non-interactive flow driven by `gwscript --setup CHISELTOP`."""
    if "." not in chiseltop:
        raise RuntimeError(
            "chiseltop must be '<package>.<ClassName>', "
            f"e.g. chiseltop=srv32i.AxiSRV32I (got: {chiseltop})"
        )

    print(f"No .gwconfig found in {root}; running automated setup (--setup {chiseltop}).")
    return _scaffold(root, config_file, chiseltop, "gwtests")
