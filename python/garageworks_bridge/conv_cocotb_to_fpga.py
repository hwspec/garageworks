#!/usr/bin/env python3
"""
Convert a set of AXI cocotb testbench files into FPGA testbench files.

Usage:
    python conv_cocotb_to_fpga.py tb_filter.py r2bridge.py subtest1.py

Each input file `foo.py` is written as `fpga_foo.py` (default: same
directory as input, or --outdir if given). Local imports between files
in the same batch are rewritten to match (e.g. `from r2bridge import
R2Bridge` -> `from fpga_r2bridge import R2Bridge`) so the converted
files import each other correctly.

To overwrite existing output files, use the --force option.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import libcst as cst
import libcst.matchers as m


def fpga_name(stem: str) -> str:
    """Map a module/file stem to its converted-output equivalent name."""
    return f"fpga_{stem}"


class CocotbToFpgaTransformer(cst.CSTTransformer):
    def __init__(
        self,
        fpga: str,
        local_stems: frozenset[str] = frozenset(),
        drop_setup: bool = True,
    ):
        self.sim = 'COCOTB'
        self.fpga = fpga
        self.local_stems = local_stems
        self.drop_setup = drop_setup
        self.generated_main = False
        self._bridge_class_depth: list[bool] = []

    def _module_name(self, node: cst.BaseExpression) -> str:
        """Return dotted module name from LibCST Name/Attribute node."""
        if isinstance(node, cst.Name):
            return node.value
        if isinstance(node, cst.Attribute):
            left = self._module_name(node.value)
            return f"{left}.{node.attr.value}"
        return ""

    # -------------------------
    # Imports
    # -------------------------
    def leave_Import(
        self,
        original_node: cst.Import,
        updated_node: cst.Import,
    ) -> cst.Import | cst.RemovalSentinel:
        kept = []
        for alias in updated_node.names:
            name = alias.name
            # remove: import cocotb
            if isinstance(name, cst.Name) and name.value == "cocotb":
                continue
            kept.append(alias.with_changes(comma=cst.MaybeSentinel.DEFAULT))
        if not kept:
            return cst.RemoveFromParent()
        return updated_node.with_changes(names=kept)

    def leave_ImportFrom(
        self,
        original_node: cst.ImportFrom,
        updated_node: cst.ImportFrom,
    ) -> cst.ImportFrom | cst.RemovalSentinel:
        if updated_node.module is None:
            return updated_node

        module_code = self._module_name(updated_node.module)

        # remove: from cocotb... import ...
        if module_code == "cocotb" or module_code.startswith("cocotb."):
            return cst.RemoveFromParent()

        # e.g.,
        #   from axi_test_bridge.cocotb_bridge import COCOTB_Bridge
        # ->
        #   from axi_test_bridge.aved_bridge import AVED_Bridge
        if module_code == "axi_test_bridge.cocotb_bridge":
            return updated_node.with_changes(
                module=cst.Attribute(
                    value=cst.Name("axi_test_bridge"),
                    attr=cst.Name(f"{self.fpga.lower()}_bridge"),
                ),
                names=[
                    cst.ImportAlias(
                        name=cst.Name(f"{self.fpga}_Bridge"),
                        comma=cst.MaybeSentinel.DEFAULT,
                    )
                ],
            )

        # Batch-local import rewrite, e.g.
        #   from r2bridge import R2Bridge
        # ->
        #   from fpga_r2bridge import R2Bridge
        # (also covers helper modules like subtest1.py)
        if module_code in self.local_stems:
            return updated_node.with_changes(
                module=cst.Name(fpga_name(module_code)),
            )

        return updated_node

    # -------------------------
    # async / await conversion
    # -------------------------
    def leave_Await(
        self,
        original_node: cst.Await,
        updated_node: cst.Await,
    ) -> cst.BaseExpression:
        # await rev.readWord(...)
        # ->
        # rev.readWord(...)
        return updated_node.expression

    # -------------------------
    # Function conversion
    # -------------------------
    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        decorators = []
        is_cocotb_test = False
        for dec in updated_node.decorators:
            if self._is_cocotb_test_decorator(dec):
                is_cocotb_test = True
                continue
            decorators.append(dec)

        # async def -> def
        new_node = updated_node.with_changes(
            asynchronous=None,
            decorators=decorators,
        )

        if is_cocotb_test:
            self.generated_main = True
            # async def sim_simple(dut):
            # ->
            # def main():
            new_node = new_node.with_changes(
                name=cst.Name("main"),
                params=cst.Parameters(),
            )

        # Inside a *Bridge subclass, __init__(self, cocotb_dut) drops the
        # cocotb_dut param, since the FPGA-side bridge takes none.
        in_bridge_class = (
            self._bridge_class_depth and self._bridge_class_depth[-1]
        )
        if in_bridge_class and updated_node.name.value == "__init__":
            params = list(updated_node.params.params)
            if len(params) >= 2:
                # keep self and all params after cocotb_dut
                new_params_list = [params[0]] + params[2:]
                new_params = updated_node.params.with_changes(
                    params = new_params_list
                    )
                new_node =  new_node.with_changes(params=new_params)

        return new_node

    def _is_cocotb_test_decorator(self, dec: cst.Decorator) -> bool:
        expr = dec.decorator
        # @cocotb.test
        if m.matches(
            expr,
            m.Attribute(
                value=m.Name("cocotb"),
                attr=m.Name("test"),
            ),
        ):
            return True
        # @cocotb.test()
        if m.matches(
            expr,
            m.Call(
                func=m.Attribute(
                    value=m.Name("cocotb"),
                    attr=m.Name("test"),
                )
            ),
        ):
            return True
        return False

    # -------------------------
    # Class definition: rebase project bridge classes
    # e.g. class R2Bridge(COCOTB_Bridge): -> class R2Bridge(AVED_Bridge):
    # -------------------------
    def _is_bridge_base(self, base_value: cst.BaseExpression) -> bool:
        name = self._module_name(base_value)
        return name == "COCOTB_Bridge" or name.endswith("Bridge")

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        is_bridge_class = any(
            self._is_bridge_base(base.value) for base in node.bases
        )
        self._bridge_class_depth.append(is_bridge_class)

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        self._bridge_class_depth.pop()

        new_bases = []
        changed = False
        for base in updated_node.bases:
            if m.matches(base.value, m.Name("COCOTB_Bridge")):
                new_bases.append(
                    base.with_changes(value=cst.Name(f"{self.fpga}_Bridge"))
                )
                changed = True
            else:
                new_bases.append(base)
        if changed:
            return updated_node.with_changes(bases=new_bases)
        return updated_node

    # -------------------------
    # Class replacement (generalized to any *Bridge subclass call)
    # -------------------------
    def leave_Call(
        self,
        original_node: cst.Call,
        updated_node: cst.Call,
    ) -> cst.Call:
        def drop_cocotb_dut(args):
            args = list(args)
            if (
                    args
                    and args[0].keyword is None
                    and isinstance(args[0].value, cst.Name)
                    and args[0].value.value == "cocotb_dut"
            ):
                return args[1:]
            return args

        if isinstance(updated_node.func, cst.Name):
            name = updated_node.func.value

            # COCOTB_Bridge(cocotb_dut) -> AVED_Bridge()
            if name == "COCOTB_Bridge":
                return updated_node.with_changes(
                    func=cst.Name(f"{self.fpga}_Bridge"),
                    args=[],
                )

            # R2Bridge(cocotb_dut, ...) -> R2Bridge(...)
            if name.endswith("Bridge") and name != f"{self.fpga}_Bridge":
                return updated_node.with_changes(
                    args=drop_cocotb_dut(updated_node.args)
                )

        # Inside a *Bridge subclass, super().__init__(cocotb_dut, ...) -> super().__init__(...)
        in_bridge_class = (
            self._bridge_class_depth and self._bridge_class_depth[-1]
        )
        if in_bridge_class and m.matches(
            updated_node.func,
            m.Attribute(
                value=m.Call(func=m.Name("super")),
                attr=m.Name("__init__"),
            ),
        ):
            return updated_node.with_changes(
                args=drop_cocotb_dut(updated_node.args)
            )

        return updated_node

    # -------------------------
    # Optional setup removal
    # -------------------------
    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
        if self.drop_setup and len(updated_node.body) == 1:
            stmt = updated_node.body[0]
            # Drop:
            #   await rev.setup()
            # After await-removal, this becomes:
            #   rev.setup()
            if m.matches(
                stmt,
                m.Expr(
                    value=m.Call(
                        func=m.Attribute(
                            attr=m.Name("setup"),
                        )
                    )
                ),
            ):
                return cst.RemoveFromParent()
        return updated_node

    # -------------------------
    # Add main entry point
    # -------------------------
    def leave_Module(
        self,
        original_node: cst.Module,
        updated_node: cst.Module,
    ) -> cst.Module:
        if not self.generated_main:
            return updated_node

        main_block = cst.parse_statement(
            '''
if __name__ == "__main__":
    main()
'''
        )
        return updated_node.with_changes(
            body=list(updated_node.body) + [main_block]
        )


def convert_code(
    source: str,
    fpga: str,
    local_stems: frozenset[str] = frozenset(),
    drop_setup: bool = True,
) -> str:
    module = cst.parse_module(source)
    transformed = module.visit(
        CocotbToFpgaTransformer(
            fpga=fpga,
            local_stems=local_stems,
            drop_setup=drop_setup,
        )
    )
    return transformed.code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a batch of restricted cocotb testbench files "
                     "to FPGA testbench files."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Input cocotb files, e.g. tb_filter.py r2bridge.py subtest1.py",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Directory for converted output files "
             "(default: same directory as each input file).",
    )
    parser.add_argument(
        "--fpga",
        default="AVED",
        help="FPGA backend name (default: %(default)s)",
    )
    parser.add_argument(
        "--keep-setup",
        action="store_true",
        help="Keep setup() calls instead of removing them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output files if they already exist.",
    )
    args = parser.parse_args()

    # Batch-wide set of local module stems, so local imports between
    # files in this same invocation get rewritten consistently
    # (e.g. "r2bridge", "subtest1").
    local_stems = frozenset(p.stem for p in args.inputs)

    # Resolve output paths up front and check for collisions before
    # writing anything.
    output_paths = []
    for in_path in args.inputs:
        outdir = args.outdir if args.outdir is not None else in_path.parent
        out_path = outdir / f"{fpga_name(in_path.stem)}{in_path.suffix}"
        if out_path.exists() and not args.force:
            raise FileExistsError(
                f"{out_path} already exists. Use --force to overwrite."
            )
        output_paths.append(out_path)

    for in_path, out_path in zip(args.inputs, output_paths):
        source = in_path.read_text()
        converted = convert_code(
            source=source,
            fpga=args.fpga,
            local_stems=local_stems,
            drop_setup=False,
#           drop_setup=not args.keep_setup,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(converted)
        print(f"{in_path} -> {out_path}")


if __name__ == "__main__":
    main()
