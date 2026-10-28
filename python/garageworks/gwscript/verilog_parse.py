"""Parser for "simple" ANSI-style Verilog module headers.

Supports exactly the shape used by verilog/Cmd/Dut.v: a single
`module NAME #(params)(ports);` header with flat `input`/`output`/`inout`
`wire` ports (no interfaces, structs, or generate blocks). This is a
best-effort fact-extractor that seeds an LLM prompt for `--gwcd`; the real
correctness gate is `sbt compile`/`sbt test`, not this parser.
"""

import re


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//.*", "", text)
    return text


def _split_commas(s):
    """Split on top-level commas only (ignore commas inside [] {} ())."""
    depth = 0
    buf = []
    out = []
    for ch in s:
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


_PARAM_RE = re.compile(
    r"parameter\s+(?:(integer|real|signed|unsigned)\s+)?(\w+)\s*=\s*(.+)"
)
_PARAM_CONT_RE = re.compile(r"(\w+)\s*=\s*(.+)")
_PORT_RE = re.compile(
    r"(input|output|inout)?\s*(wire|reg|logic)?\s*(signed)?\s*"
    r"(\[\s*([^\]]+?)\s*:\s*([^\]]+?)\s*\])?\s*(\w+)\s*$"
)


def parse_module(text, want_name=None):
    """Parse a single simple ANSI-style Verilog module.

    Returns (name, params, ports):
      params: [{"type", "name", "default"}, ...]
      ports: [{"direction", "msb", "lsb", "width_expr", "name"}, ...]

    Raises ValueError with an actionable message when the module doesn't
    match the supported "simple" shape (no ANSI header, ambiguous module
    name, unparseable port declaration, etc).
    """
    text = _strip_comments(text)

    mods = {}
    for m in re.finditer(r"\bmodule\s+(\w+)\s*(.*?)\bendmodule\b", text, re.S):
        mods[m.group(1)] = m.group(2)

    names = list(mods.keys())
    if want_name:
        if want_name not in mods:
            raise ValueError(f"module {want_name!r} not found; found: {names}")
        name, body = want_name, mods[want_name]
    elif len(mods) == 1:
        name, body = names[0], mods[names[0]]
    elif not mods:
        raise ValueError("no 'module ... endmodule' block found")
    else:
        raise ValueError(
            f"multiple modules found ({names}); topmodule must be specified "
            "with TARGET:TOP"
        )

    header_m = re.match(r"\s*(#\s*\((.*?)\))?\s*\((.*?)\)\s*;", body, re.S)
    if not header_m:
        raise ValueError(
            f"module {name!r}: could not find an ANSI-style port list header "
            "(`module NAME #(params)(ports);`) -- only simple ANSI-style "
            "modules are supported"
        )
    param_block, port_block = header_m.group(2) or "", header_m.group(3)

    params = []
    for item in _split_commas(param_block):
        item = item.strip()
        if not item:
            continue
        pm = _PARAM_RE.match(item)
        if pm:
            params.append(
                {"type": pm.group(1) or "", "name": pm.group(2), "default": pm.group(3).strip()}
            )
        else:
            # continuation entry sharing a prior item's 'parameter' keyword,
            # e.g. "parameter BW = 32, BW2 = 8"
            pm2 = _PARAM_CONT_RE.match(item)
            if pm2:
                params.append({"type": "", "name": pm2.group(1), "default": pm2.group(2).strip()})
            else:
                raise ValueError(f"module {name!r}: could not parse parameter decl: {item!r}")

    ports = []
    last_dir = None
    for item in _split_commas(port_block):
        item = item.strip()
        if not item:
            continue
        pm = _PORT_RE.match(item)
        if not pm:
            raise ValueError(
                f"module {name!r}: could not parse port decl: {item!r} -- only "
                "flat input/output/inout wire ports are supported"
            )
        direction = pm.group(1) or last_dir
        if direction is None:
            raise ValueError(f"module {name!r}: port {item!r} has no direction and none inherited")
        last_dir = direction
        msb, lsb = pm.group(5), pm.group(6)
        ports.append(
            {
                "direction": direction,
                "msb": msb,
                "lsb": lsb,
                "width_expr": f"{msb}-({lsb})+1" if msb else "1",
                "name": pm.group(7),
            }
        )

    if not ports:
        raise ValueError(f"module {name!r}: no ports found")

    return name, params, ports
