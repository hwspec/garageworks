"""`gwscript --gwcd TARGET[:TOP]`: use an LLM (Anthropic primary, OpenAI
fallback) to generate a GarageWorks Command Dispatcher (GWCD) -- an
AXI4-Lite wrapper Chisel module plus a ScalaTest spec -- around a plain
Chisel DUT or a "simple" ANSI-style Verilog module, then bootstrap an sbt
project if needed and compile/test the result with a bounded repair loop.

Python computes every file path involved; the LLM is only ever asked for
two string fields (wrapper_scala, spec_scala). See gwcdskills/ for the
static reference material included in every prompt.
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from . import config
from . import setup_wizard
from . import verilog_parse
from .shell import capture

MAX_ATTEMPTS = 3
MAX_FIELD_BYTES = 200_000
PRIME_COMPILE_TIMEOUT = 900  # seconds; first sbt compile resolves dependencies

_TOP_RE = re.compile(r"^[A-Za-z_]\w*$")
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)
_CLASS_RE_STRICT = re.compile(
    r"\bclass\s+(\w+)\b(?:\s*\[[^\]]*\])?(?:\s*\([^{]*?\))?\s*extends\s+(Module|RawModule|BlackBox|ExtModule)\b",
    re.DOTALL,
)
_CLASS_RE_ANY = re.compile(
    r"\bclass\s+(\w+)\b(?:\s*\[[^\]]*\])?(?:\s*\([^{]*?\))?\s*extends\s+([\w.]+)",
    re.DOTALL,
)
_NON_DUT_BASES = {"Bundle", "Record"}

SCHEMA = {
    "type": "object",
    "properties": {
        "wrapper_scala": {"type": "string"},
        "spec_scala": {"type": "string"},
    },
    "required": ["wrapper_scala", "spec_scala"],
    "additionalProperties": False,
}


class GwcdError(RuntimeError):
    pass


def run_gwcd(target_spec):
    provider = _pick_provider()
    _check_tools()

    target_str, top_hint = _parse_target_spec(target_spec)
    target_path = Path(target_str)
    if not target_path.is_file():
        raise GwcdError(f"--gwcd target not found: {target_path}")

    if target_path.suffix in (".v", ".sv"):
        facts = _extract_verilog_facts(target_path, top_hint)
    elif target_path.suffix == ".scala":
        facts = _extract_chisel_facts(target_path, top_hint)
    else:
        raise GwcdError(
            "--gwcd target must be a .scala (Chisel) or .v/.sv (Verilog) file "
            f"(got: {target_path.suffix or '<none>'})"
        )

    root = config.ROOT
    log_dir = root / ".gwlogs" / f"gwcd-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    log_dir.mkdir(parents=True, exist_ok=True)

    _bootstrap_sbt_project(root)
    _place_target_source(root, target_path, facts)
    _prime_sbt(root, log_dir)

    wrapper_path = root / "src" / "main" / "scala" / facts["pkg_path"] / f"{facts['top']}GWCD.scala"
    spec_path = root / "src" / "test" / "scala" / facts["pkg_path"] / f"{facts['top']}GWCDSpec.scala"
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.parent.mkdir(parents=True, exist_ok=True)

    static_ctx = _build_static_context()

    retry = None
    last_stage = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = _build_prompt(static_ctx, facts, attempt, retry)
        data = _call_llm(provider, prompt, log_dir, attempt)

        wrapper_path.write_text(data["wrapper_scala"])
        spec_path.write_text(data["spec_scala"])

        ok, stage, output = _compile_and_test(root, log_dir, attempt)
        if ok:
            print(
                f"\n--gwcd: {facts['pkg']}.{facts['top']}GWCD compiled and tested "
                f"successfully (attempt {attempt}/{MAX_ATTEMPTS})."
            )
            _finish_setup(root, facts)
            return

        last_stage = stage
        summary = _summarize_build_output(output, stage)
        retry = {
            "wrapper": data["wrapper_scala"],
            "spec": data["spec_scala"],
            "text": summary,
        }
        print(
            f"\n--gwcd: attempt {attempt}/{MAX_ATTEMPTS} failed at `sbt {stage}`"
            + ("; retrying." if attempt < MAX_ATTEMPTS else "; giving up.")
        )

    raise GwcdError(
        f"--gwcd: giving up after {MAX_ATTEMPTS} attempts (last failure: sbt {last_stage}).\n"
        f"Generated files were left in place for inspection:\n"
        f"  {wrapper_path}\n  {spec_path}\n"
        f"Full attempt logs: {log_dir}"
    )


# ---------------------------------------------------------------------------
# Target parsing / provider / tool checks
# ---------------------------------------------------------------------------

def _parse_target_spec(spec):
    if ":" in spec:
        target, _, top = spec.rpartition(":")
        if target and _TOP_RE.match(top):
            return target, top
    return spec, None


def _pick_provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    raise GwcdError(
        "--gwcd requires an API key: set ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY."
    )


def _check_tools():
    missing = [tool for tool in ("java", "sbt") if not shutil.which(tool)]
    if missing:
        raise GwcdError(f"--gwcd needs {' and '.join(missing)} on PATH (not found).")


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def _pkg_from_stem(stem):
    pkg = re.sub(r"[^0-9A-Za-z_]", "", stem).lower()
    if not pkg or pkg[0].isdigit():
        pkg = f"g{pkg}" if pkg else "gwcdtarget"
    return pkg


def _extract_verilog_facts(path, top_hint):
    text = path.read_text(errors="replace")
    try:
        name, params, ports = verilog_parse.parse_module(text, want_name=top_hint)
    except ValueError as e:
        raise GwcdError(
            f"--gwcd: could not parse {path} as a simple ANSI-style Verilog module: {e}\n"
            "Only simple modules (flat input/output wire ports, no interfaces/structs/"
            "generate blocks) are supported; rewrite the target, point --gwcd at a Chisel "
            "source instead, or pass TARGET:TOP to disambiguate."
        )
    top = top_hint or name
    pkg = _pkg_from_stem(path.stem)
    return {
        "kind": "verilog",
        "top": top,
        "pkg": pkg,
        "pkg_path": Path(*pkg.split(".")),
        "reset_style": "extmodule_direct",
        "reset_style_guessed": False,
        "verilog_params": params,
        "verilog_ports": ports,
        "src_filename": path.name,
        "src_text": text,
        "verilog_rel_path": f"verilog/{top}/{path.name}",
    }


def _chisel_candidates(text):
    strict = [(m.group(1), m.group(2)) for m in _CLASS_RE_STRICT.finditer(text)]
    if strict:
        return strict, False
    any_ = [
        (m.group(1), m.group(2))
        for m in _CLASS_RE_ANY.finditer(text)
        if m.group(2).split(".")[-1] not in _NON_DUT_BASES
    ]
    return any_, True


def _extract_chisel_facts(path, top_hint):
    text = path.read_text(errors="replace")
    pkg_match = _PACKAGE_RE.search(text)
    inserted_package = None
    if pkg_match:
        pkg = pkg_match.group(1)
    else:
        pkg = _pkg_from_stem(path.stem)
        inserted_package = pkg
        text = f"package {pkg}\n\n{text}"

    candidates, guessed = _chisel_candidates(text)
    names = [c[0] for c in candidates]
    by_name = dict(candidates)

    if top_hint:
        if top_hint not in by_name:
            raise GwcdError(
                f"--gwcd: class {top_hint!r} not found in {path}; found: {names or 'none'}"
            )
        top = top_hint
    elif len(candidates) == 1:
        top = names[0]
    elif not candidates:
        raise GwcdError(
            f"--gwcd: no DUT-shaped class found in {path} (expected a class extending "
            "Module/RawModule/BlackBox/ExtModule); specify TARGET:TOP or point at a "
            "different file"
        )
    else:
        stem_matches = [n for n in names if n.lower() == path.stem.lower()]
        if len(stem_matches) == 1:
            top = stem_matches[0]
        else:
            raise GwcdError(
                f"--gwcd: multiple candidate DUT classes found in {path}: "
                f"{', '.join(names)}; specify one with TARGET:TOP"
            )

    base = by_name[top]
    base_simple = base.split(".")[-1]
    reset_style = "extmodule_direct" if base_simple in ("BlackBox", "ExtModule") else "withreset_wrap"
    reset_style_guessed = guessed or base_simple not in ("Module", "RawModule", "BlackBox", "ExtModule")

    return {
        "kind": "chisel",
        "top": top,
        "pkg": pkg,
        "pkg_path": Path(*pkg.split(".")),
        "reset_style": reset_style,
        "reset_style_guessed": reset_style_guessed,
        "src_filename": path.name,
        "src_text": text,
        "inserted_package": inserted_package,
    }


# ---------------------------------------------------------------------------
# Project bootstrap
# ---------------------------------------------------------------------------

# Keep in sync with gwcdskills/build_sbt.tmpl's own hardcoded version.
GARAGEWORKS_VERSION = "0.4.0-SNAPSHOT"


def _ensure_garageworks_dep(build_sbt):
    """An existing (user-authored) build.sbt may not depend on garageworks
    yet -- without it, the LLM's `import garageworks._` can't resolve, and
    the repair loop has been observed "fixing" that compile error by
    vendoring garageworks source into the generated file instead of adding
    the dependency (which it has no way to do itself). Append it if
    missing; never touch a build.sbt that already has it."""
    text = build_sbt.read_text()
    if "garageworks" in text:
        return
    addition = (
        "\n// Added by `gwscript --gwcd` -- needed to resolve `import garageworks._`.\n"
        'resolvers += "central-snapshots" at "https://central.sonatype.com/repository/maven-snapshots/"\n'
        f'libraryDependencies += "io.github.hwspec" %% "garageworks" % "{GARAGEWORKS_VERSION}"\n'
    )
    build_sbt.write_text(text.rstrip("\n") + "\n" + addition)
    print(f"Added garageworks dependency to {build_sbt}")


def _bootstrap_sbt_project(root):
    skills = files("garageworks.gwscript") / "gwcdskills"

    build_sbt = root / "build.sbt"
    if not build_sbt.exists():
        tmpl = (skills / "build_sbt.tmpl").read_text()
        build_sbt.write_text(tmpl.format(name=root.name))
        print(f"Wrote {build_sbt}")
    else:
        _ensure_garageworks_dep(build_sbt)

    proj_dir = root / "project"
    proj_dir.mkdir(exist_ok=True)

    props = proj_dir / "build.properties"
    if not props.exists():
        props.write_text((skills / "project_build.properties").read_text())
        print(f"Wrote {props}")

    plugins = proj_dir / "plugins.sbt"
    if not plugins.exists():
        plugins.write_text((skills / "project_plugins.sbt").read_text())
        print(f"Wrote {plugins}")


def _place_target_source(root, target_path, facts):
    if facts["kind"] == "chisel":
        dst_dir = root / "src" / "main" / "scala" / facts["pkg_path"]
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / facts["src_filename"]
        if not dst.exists():
            dst.write_text(facts["src_text"])
            print(f"Wrote {dst}")
    else:
        verilog_dir = root / "verilog" / facts["top"]
        verilog_dir.mkdir(parents=True, exist_ok=True)
        dst = verilog_dir / facts["src_filename"]
        if not dst.exists():
            shutil.copy(target_path, dst)
            print(f"Wrote {dst}")


def _prime_sbt(root, log_dir):
    """Run an initial `sbt compile` before any LLM-generated code exists, so
    dependency resolution happens once with a generous timeout, outside the
    repair-attempt budget."""
    print("\n--gwcd: resolving dependencies (first `sbt compile`, this can take a while)...")
    try:
        out = capture(["sbt", "-batch", "compile"], cwd=root, timeout=PRIME_COMPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GwcdError(
            f"--gwcd: `sbt compile` timed out after {PRIME_COMPILE_TIMEOUT}s while resolving "
            "dependencies. Check network access to Maven Central / Sonatype snapshots and retry."
        )
    except subprocess.CalledProcessError as e:
        log_path = log_dir / "prime-compile.log"
        log_path.write_text(e.output or "")
        raise GwcdError(
            f"--gwcd: the target source alone failed to compile before GWCD generation "
            f"even started; see {log_path}"
        )
    (log_dir / "prime-compile.log").write_text(out)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_static_context():
    skills = files("garageworks.gwscript") / "gwcdskills"
    return {
        "template": (skills / "GWCmdTemplate.scala").read_text(),
        "axigcd_example": (skills / "AxiGCD_example.scala").read_text(),
        "axigcd_spec_example": (skills / "AxiGCDSpec_example.scala").read_text(),
        "conventions": (skills / "conventions.md").read_text(),
        "bfm_and_cocotb": (skills / "bfm_and_cocotb.md").read_text(),
    }


def _build_prompt(static_ctx, facts, attempt, retry):
    top = facts["top"]
    pkg = facts["pkg"]
    wrapper_class = f"{top}GWCD"
    spec_class = f"{wrapper_class}Spec"

    lines = [
        "You generate a GarageWorks Command Dispatcher (GWCD): an AXI4-Lite "
        "wrapper Chisel module around a DUT, plus a ScalaTest spec for it. "
        "Follow the conventions and examples below exactly; they define the "
        "only supported patterns.",
        "\n## Conventions\n" + static_ctx["conventions"],
        "\n## BFM and cocotb API\n" + static_ctx["bfm_and_cocotb"],
        "\n## Canonical example 1 (ExtModule/Verilog DUT, extmodule_direct "
        "reset style)\n```scala\n" + static_ctx["template"] + "\n```",
        "\n## Canonical example 2 (plain Module DUT, withreset_wrap reset "
        "style)\n```scala\n" + static_ctx["axigcd_example"] + "\n```\n"
        "```scala\n" + static_ctx["axigcd_spec_example"] + "\n```",
        "\n## Target",
        f"- Output package: `{pkg}`",
        f"- Wrapper class name: `{wrapper_class}` (with a companion `object {wrapper_class} extends App`)",
        f"- Spec class name: `{spec_class}`",
        "- Reset wiring style to use: **" + facts["reset_style"] + "**"
        + (
            " (best-effort guess from source inspection; if compilation shows "
            "this is wrong, use the other style instead)"
            if facts.get("reset_style_guessed")
            else ""
        ),
    ]

    if facts["kind"] == "chisel":
        lines.append(
            f"- DUT: Chisel class `{facts['top']}`, already present in this same "
            f"package (`{pkg}`) as the file below -- do not redefine it, just "
            "instantiate it as the examples do."
        )
        lines.append(
            f"\n### DUT source (`{facts['src_filename']}`)\n```scala\n{facts['src_text']}\n```"
        )
    else:
        lines.append(
            f"- DUT: Verilog module `{facts['top']}`, available via "
            f"`addPath(\"{facts['verilog_rel_path']}\")` on an `ExtModule`/`BlackBox` "
            "class you define in wrapper_scala (same as `Dut` in canonical example 1)."
        )
        lines.append("- Parsed parameters: " + json.dumps(facts.get("verilog_params", [])))
        lines.append("- Parsed ports: " + json.dumps(facts.get("verilog_ports", [])))
        lines.append(
            f"\n### Verilog source (`{facts['src_filename']}`)\n```verilog\n{facts['src_text']}\n```"
        )

    lines.append(
        f"\n## Output\nReturn exactly two Scala files as the two JSON fields:\n"
        f"- `wrapper_scala`: the full contents of `{wrapper_class}.scala` "
        f"(package `{pkg}`; includes the params case class + companion object, "
        f"the `{wrapper_class}` module, and an `object {wrapper_class} extends App` "
        "that calls `EmitVerilog.generate`, exactly like the canonical examples).\n"
        f"- `spec_scala`: the full contents of `{spec_class}.scala` "
        f"(package `{pkg}`; a ScalaTest spec using `Axi4Lite32BFM`, exactly like "
        "canonical example 2's spec)."
    )

    if retry:
        lines.append(
            f"\n## Previous attempt failed (attempt {attempt - 1} of {MAX_ATTEMPTS})\n"
            "Here is the current (broken) content you must fix:\n\n"
            f"### wrapper_scala\n```scala\n{retry['wrapper']}\n```\n"
            f"### spec_scala\n```scala\n{retry['spec']}\n```\n\n"
            f"### Build error\n```\n{retry['text']}\n```\n"
            "Fix the specific problem shown above; do not otherwise rewrite "
            "working parts of the files."
        )

    lines.append(f"\nThis is attempt {attempt} of {MAX_ATTEMPTS}.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def _with_infra_retry(fn, retry_exceptions, tries=3, base_delay=2.0):
    for i in range(tries):
        try:
            return fn()
        except retry_exceptions:
            if i == tries - 1:
                raise
            time.sleep(base_delay * (2 ** i))


def _call_anthropic(prompt, max_tokens=16000):
    try:
        import anthropic
    except ImportError:
        raise GwcdError("--gwcd needs the 'anthropic' package: pip install 'garageworks[gwcd]'")

    client = anthropic.Anthropic()

    def _do():
        with client.messages.stream(
            model="claude-opus-5-5",
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_message()

    message = _with_infra_retry(
        _do, (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
    )

    if message.stop_reason == "max_tokens":
        if max_tokens >= 64000:
            raise GwcdError("--gwcd: Anthropic response truncated even at 64000 max_tokens")
        return _call_anthropic(prompt, max_tokens=max_tokens * 2)

    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise GwcdError("--gwcd: Anthropic response contained no text block")
    return json.loads(text)


def _call_openai(prompt, max_tokens=16000):
    try:
        import openai
    except ImportError:
        raise GwcdError("--gwcd needs the 'openai' package: pip install 'garageworks[gwcd]'")

    client = openai.OpenAI()
    model = os.environ.get("OPENAI_GWCD_MODEL", "gpt-5")

    def _do():
        return client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "emit_gwcd_files",
                    "schema": SCHEMA,
                    "strict": True,
                },
            },
        )

    response = _with_infra_retry(
        _do, (openai.RateLimitError, openai.APIStatusError, openai.APIConnectionError)
    )

    choice = response.choices[0]
    if choice.finish_reason == "length":
        if max_tokens >= 64000:
            raise GwcdError("--gwcd: OpenAI response truncated even at 64000 max_completion_tokens")
        return _call_openai(prompt, max_tokens=max_tokens * 2)

    return json.loads(choice.message.content)


def _call_llm(provider, prompt, log_dir, attempt):
    (log_dir / f"attempt-{attempt}.prompt.txt").write_text(prompt)

    if provider == "anthropic":
        data = _call_anthropic(prompt)
    else:
        data = _call_openai(prompt)

    for key in ("wrapper_scala", "spec_scala"):
        if key not in data or not isinstance(data[key], str):
            raise GwcdError(f"--gwcd: LLM response missing/invalid {key!r} field")
        if len(data[key].encode("utf-8")) > MAX_FIELD_BYTES:
            raise GwcdError(f"--gwcd: LLM response {key!r} field exceeds {MAX_FIELD_BYTES} bytes")

    (log_dir / f"attempt-{attempt}.wrapper.scala").write_text(data["wrapper_scala"])
    (log_dir / f"attempt-{attempt}.spec.scala").write_text(data["spec_scala"])
    return data


# ---------------------------------------------------------------------------
# Compile / test / repair loop support
# ---------------------------------------------------------------------------

def _compile_and_test(root, log_dir, attempt):
    try:
        out = capture(["sbt", "-batch", "compile"], cwd=root)
    except subprocess.CalledProcessError as e:
        (log_dir / f"attempt-{attempt}.compile.log").write_text(e.output or "")
        return False, "compile", e.output or ""
    (log_dir / f"attempt-{attempt}.compile.log").write_text(out)

    try:
        out = capture(["sbt", "-batch", "test"], cwd=root)
    except subprocess.CalledProcessError as e:
        (log_dir / f"attempt-{attempt}.test.log").write_text(e.output or "")
        return False, "test", e.output or ""
    (log_dir / f"attempt-{attempt}.test.log").write_text(out)
    return True, None, out


def _summarize_build_output(text, kind, max_chars=6000, max_blocks=20):
    if kind == "compile":
        lines = text.splitlines()
        blocks = []
        i = 0
        while i < len(lines) and len(blocks) < max_blocks:
            if lines[i].startswith("[error]"):
                block = [lines[i]]
                i += 1
                while i < len(lines) and (
                    lines[i].startswith("[error]")
                    or (lines[i].strip() and not lines[i].startswith("["))
                ):
                    block.append(lines[i])
                    i += 1
                blocks.append("\n".join(block))
            else:
                i += 1
        summary = "\n\n".join(blocks) or text[-max_chars:]
    else:  # test
        kept = []
        capturing = False
        for line in text.splitlines():
            if "at org.scalatest." in line or line.strip().startswith("at org.scalatest."):
                continue
            if "*** FAILED ***" in line or "Exception" in line or "assert" in line.lower():
                capturing = True
            if capturing:
                kept.append(line)
        summary = "\n".join(kept) or text[-max_chars:]
    return summary[:max_chars]


def _finish_setup(root, facts):
    chiseltop = f"{facts['pkg']}.{facts['top']}GWCD"
    config_file = root / ".gwconfig"
    if config_file.exists():
        print(f"\n--gwcd: {config_file} already exists; new GWCD is chiseltop={chiseltop}")
        print("Update .gwconfig's chiseltop= yourself if you want gwscript to use it.")
        return
    setup_wizard.run_automated_setup(root, config_file, chiseltop)
    print("Setup complete. Re-run gwscript to start the stages.")
