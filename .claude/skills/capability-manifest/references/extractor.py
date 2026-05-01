#!/usr/bin/env python3
"""Capability manifest extractor for application_sdk.

Three subcommands:
  dump       -- Load via griffe, print raw JSON to stdout
  normalize  -- Filter/sort/extract from raw JSON, print normalized JSON to stdout
  render     -- Render markdown manifest from normalized JSON + purposes YAML

Usage (from repo root):
  uv run --with griffe python .claude/skills/capability-manifest/references/extractor.py dump
  uv run --with griffe python .claude/skills/capability-manifest/references/extractor.py normalize /tmp/capability-manifest/raw.json
  uv run --with griffe python .claude/skills/capability-manifest/references/extractor.py render /tmp/capability-manifest/normalized.json .claude/skills/capability-manifest/references/subpackage-purposes.yaml
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------

REPO_ROOT = (
    Path(__file__).resolve().parents[4]
)  # .claude/skills/capability-manifest/references/extractor.py
PKG_ROOT = REPO_ROOT / "application_sdk"

# Subpackages to index (those that can have __all__)
SUBPACKAGES = [
    "app",
    "clients",
    "common",
    "contracts",
    "credentials",
    "execution",
    "handler",
    "infrastructure",
    "observability",
    "outputs",
    "server",
    "storage",
    "templates",
    "testing",
    "transformers",
]

# Names treated as decorators (returned callables)
DECORATOR_NAMES = {"task", "entrypoint", "on_event"}

# Contract namespaces to catalog (griffe module paths)
CONTRACT_NAMESPACES = [
    "application_sdk.contracts",
    "application_sdk.handler.contracts",
    "application_sdk.templates.contracts",
]

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def eprint(*args: Any, **kwargs: Any) -> None:
    """Print to stderr."""
    print(*args, file=sys.stderr, **kwargs)  # noqa: T201


def get_source_sha() -> str:
    """Return the git SHA of the last commit touching application_sdk/."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "application_sdk/"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


def get_source_date(sha: str) -> str:
    """Return the commit date (ISO 8601) of the given SHA."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI", sha],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


def get_sdk_version() -> str:
    """Read __version__ from application_sdk/version.py (no import needed)."""
    version_file = PKG_ROOT / "version.py"
    for line in version_file.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "unknown"


def extract_all_from_init(pkg_path: Path) -> list[str]:
    """Parse __all__ from a package __init__.py via AST (reliable for literal lists)."""
    init_file = pkg_path / "__init__.py"
    if not init_file.exists():
        return []
    tree = ast.parse(init_file.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        return [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)
                        ]
    return []


# ---------------------------------------------------------------------------
# Griffe helpers
# ---------------------------------------------------------------------------


def _griffe_obj_kind(obj: Any) -> str:
    """Return a simple string kind for a griffe object."""
    return str(obj.kind).split(".")[-1].upper()


def _render_annotation(ann: Any) -> str:
    """Convert a griffe annotation expression to a plain string."""
    if ann is None:
        return ""
    return str(ann)


def _render_signature(obj: Any) -> str:
    """Render a callable's signature string (≤120 chars, truncated deterministically).

    griffe's .signature() already includes the function name, e.g.:
      task(func: F | None = None, ...) -> F | Callable[[F], F]
    We use it directly; no extra name prefix.
    """
    try:
        if hasattr(obj, "signature") and callable(obj.signature):
            sig = str(obj.signature())
        elif hasattr(obj, "signature"):
            sig = str(obj.signature)
        else:
            sig = ""
    except Exception:
        sig = ""

    if not sig:
        return ""

    # Strip return annotation for conciseness (everything after the closing paren + ' -> ')
    # Keep it if it fits within 120; remove it when truncating.
    if len(sig) <= 120:
        return sig

    # Truncate: strip return annotation, then keep only first arg
    paren_pos = sig.find("(")
    if paren_pos == -1:
        return sig[:120]
    fn_name = sig[:paren_pos]
    args_part = sig[paren_pos + 1 :]
    first_arg = (
        args_part.split(",")[0].strip()
        if "," in args_part
        else args_part.split(")")[0].strip()
    )
    return fn_name + "(" + first_arg + ", ...)"


def _render_class_signature(cls_obj: Any) -> str:
    """Render a class signature from its __init__ (if available)."""
    init = cls_obj.members.get("__init__") if hasattr(cls_obj, "members") else None
    if init is None:
        return "class " + cls_obj.name
    try:
        if callable(init.signature):
            raw = str(init.signature())
        else:
            raw = str(init.signature)
        # griffe returns "__init__(self, ...)" — strip the method name prefix
        raw = raw.removeprefix("__init__")
        # raw is now "(self, ...)" — strip self
        inner = raw.lstrip("(")
        if inner.startswith("self, "):
            args = "(" + inner[len("self, ") :]
        elif inner.startswith("self)"):
            args = "()"
        else:
            args = raw  # no self found; keep as-is
        full = "class " + cls_obj.name + args
        if len(full) <= 120:
            return full
        # Truncate to first arg
        first_arg = args.lstrip("(").split(",")[0].strip()
        return "class " + cls_obj.name + "(" + first_arg + ", ...)"
    except Exception:
        return "class " + cls_obj.name


def _docstring_summary(obj: Any) -> str:
    """Extract the first sentence from a griffe object's docstring."""
    if obj.docstring is None:
        return "_(no docstring)_"
    text = obj.docstring.value.strip()
    if not text:
        return "_(no docstring)_"
    # First non-empty line, then trim at first period if it terminates a sentence
    first_line = text.split("\n")[0].strip()
    return first_line


def _relative_filepath(obj: Any) -> str:
    """Return the filepath relative to REPO_ROOT."""
    try:
        fp = Path(obj.filepath)
        return str(fp.relative_to(REPO_ROOT))
    except (TypeError, ValueError):
        return str(getattr(obj, "filepath", ""))


# ---------------------------------------------------------------------------
# Symbol classification
# ---------------------------------------------------------------------------

KIND_CLASS = "class"
KIND_DECORATOR = "decorator"
KIND_FUNCTION = "function"
KIND_CONSTANT = "constant"


def _classify_symbol(name: str, griffe_obj: Any) -> str:
    """Classify a symbol into one of the four manifest kinds."""
    if griffe_obj is None:
        return KIND_FUNCTION
    obj_kind = _griffe_obj_kind(griffe_obj)
    if obj_kind == "CLASS":
        return KIND_CLASS
    if obj_kind in ("FUNCTION", "MODULE"):
        # If it's a module, look for the same-named function inside (re-export)
        if obj_kind == "MODULE":
            inner = (
                griffe_obj.members.get(name) if hasattr(griffe_obj, "members") else None
            )
            if inner is not None:
                inner_kind = _griffe_obj_kind(inner)
                if inner_kind == "FUNCTION":
                    return KIND_DECORATOR if name in DECORATOR_NAMES else KIND_FUNCTION
                if inner_kind == "CLASS":
                    return KIND_CLASS
        return KIND_DECORATOR if name in DECORATOR_NAMES else KIND_FUNCTION
    if obj_kind == "ATTRIBUTE":
        return KIND_CONSTANT
    return KIND_FUNCTION


def _resolve_actual_obj(name: str, griffe_obj: Any) -> Any:
    """Resolve the real callable/class when griffe gives us a module-level re-export."""
    if griffe_obj is None:
        return None
    obj_kind = _griffe_obj_kind(griffe_obj)
    if obj_kind == "MODULE" and hasattr(griffe_obj, "members"):
        inner = griffe_obj.members.get(name)
        if inner is not None:
            return inner
    return griffe_obj


# ---------------------------------------------------------------------------
# DUMP subcommand
# ---------------------------------------------------------------------------


def cmd_dump() -> None:
    """Run griffe, serialise to JSON, print to stdout."""
    eprint("Loading application_sdk via griffe...")
    import griffe  # noqa: PLC0415

    pkg = griffe.load(
        "application_sdk",
        search_paths=[str(REPO_ROOT)],
        docstring_parser="google",
    )

    source_sha = get_source_sha()
    source_date = get_source_date(source_sha)
    sdk_version = get_sdk_version()

    output: dict[str, Any] = {
        "meta": {
            "sdk_version": sdk_version,
            "source_sha": source_sha,
            "source_date": source_date,
        },
        "subpackages": {},
    }

    for subpkg_name in SUBPACKAGES:
        griffe_subpkg = pkg.members.get(subpkg_name)
        if griffe_subpkg is None:
            eprint(f"  WARNING: {subpkg_name} not found in griffe output")
            continue

        pkg_path = PKG_ROOT / subpkg_name
        all_names = extract_all_from_init(pkg_path)
        if not all_names:
            eprint(f"  INFO: {subpkg_name}/__init__.py has no __all__; skipping")
            continue

        eprint(f"  {subpkg_name}: {len(all_names)} exports")

        symbols: list[dict[str, Any]] = []
        for name in all_names:
            griffe_obj = griffe_subpkg.members.get(name)
            resolved = _resolve_actual_obj(name, griffe_obj)
            kind = _classify_symbol(name, griffe_obj)

            if kind == KIND_CLASS:
                sig = (
                    _render_class_signature(resolved)
                    if resolved is not None
                    else ("class " + name)
                )
            elif kind in (KIND_FUNCTION, KIND_DECORATOR):
                sig = (
                    _render_signature(resolved)
                    if resolved is not None
                    else (name + "(...)")
                )
            else:
                # constant/enum
                ann = (
                    _render_annotation(resolved.annotation)
                    if resolved is not None and hasattr(resolved, "annotation")
                    else ""
                )
                sig = name + (": " + ann if ann else "")

            summary = (
                _docstring_summary(resolved)
                if resolved is not None
                else "_(no docstring)_"
            )
            filepath = _relative_filepath(resolved) if resolved is not None else ""

            symbols.append(
                {
                    "name": name,
                    "kind": kind,
                    "signature": sig,
                    "summary": summary,
                    "filepath": filepath,
                }
            )

        output["subpackages"][subpkg_name] = {
            "all": all_names,
            "symbols": symbols,
        }

    # Collect contracts
    eprint("Collecting contract models...")
    contracts_data: dict[str, list[dict[str, Any]]] = {}

    def collect_models_from_namespace(
        ns_path: str, griffe_root: Any
    ) -> list[dict[str, Any]]:
        """Walk a namespace in griffe and collect Pydantic model entries."""
        # Navigate to the namespace
        parts = ns_path.split(".")
        obj = griffe_root
        for part in parts[1:]:  # skip 'application_sdk'
            if obj is None or not hasattr(obj, "members"):
                return []
            obj = obj.members.get(part)
        if obj is None:
            return []

        models: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _collect_from(mod_obj: Any, canonical_ns: str) -> None:
            if mod_obj is None or not hasattr(mod_obj, "members"):
                return
            for m_name, m_obj in sorted(mod_obj.members.items()):
                if m_name.startswith("_"):
                    continue
                m_kind = _griffe_obj_kind(m_obj)
                if m_kind != "CLASS":
                    continue
                if not hasattr(m_obj, "bases"):
                    continue
                # Check if it's a Pydantic model
                bases_str = " ".join(str(b) for b in m_obj.bases)
                if not any(b in bases_str for b in ("BaseModel", "Input", "Output")):
                    continue
                uid = canonical_ns + "." + m_name
                if uid in seen:
                    continue
                seen.add(uid)
                # Extract fields (class attributes with annotations)
                # Exclude Pydantic internals and class-level config
                PYDANTIC_INTERNALS = {
                    "model_config",
                    "model_fields",
                    "model_computed_fields",
                }
                fields: list[dict[str, Any]] = []
                for f_name, f_obj in (
                    m_obj.members.items() if hasattr(m_obj, "members") else []
                ):
                    if f_name.startswith("_"):
                        continue
                    if f_name in PYDANTIC_INTERNALS:
                        continue
                    f_kind = _griffe_obj_kind(f_obj)
                    if f_kind != "ATTRIBUTE":
                        continue
                    ann = (
                        _render_annotation(f_obj.annotation)
                        if hasattr(f_obj, "annotation")
                        else ""
                    )
                    # Skip attributes with no annotation (class-level assignments without type hints)
                    if not ann:
                        continue
                    default = (
                        str(f_obj.value)
                        if hasattr(f_obj, "value") and f_obj.value is not None
                        else None
                    )
                    desc = (
                        f_obj.docstring.value.strip().split("\n")[0].strip()
                        if hasattr(f_obj, "docstring") and f_obj.docstring
                        else ""
                    )
                    fields.append(
                        {
                            "name": f_name,
                            "annotation": ann,
                            "default": default,
                            "description": desc,
                        }
                    )
                models.append(
                    {
                        "name": m_name,
                        "namespace": canonical_ns,
                        "summary": _docstring_summary(m_obj),
                        "filepath": _relative_filepath(m_obj),
                        "fields": fields,
                    }
                )

        _collect_from(obj, ns_path)
        return models

    for ns in CONTRACT_NAMESPACES:
        models = collect_models_from_namespace(ns, pkg)
        if models:
            contracts_data[ns] = models
            eprint(f"  {ns}: {len(models)} models")

    output["contracts"] = contracts_data

    print(json.dumps(output, indent=2))  # noqa: T201
    eprint("Dump complete.")


# ---------------------------------------------------------------------------
# NORMALIZE subcommand
# ---------------------------------------------------------------------------


def cmd_normalize(raw_json_path: str) -> None:
    """Filter/sort the raw dump, print normalized JSON to stdout."""
    eprint(f"Normalizing {raw_json_path}...")
    with open(raw_json_path) as f:
        raw = json.load(f)

    # Meta passes through unchanged
    normalized: dict[str, Any] = {
        "meta": raw["meta"],
        "subpackages": {},
        "contracts": {},
    }

    # Sort subpackages alphabetically
    GROUP_ORDER = {KIND_CLASS: 0, KIND_DECORATOR: 1, KIND_FUNCTION: 2, KIND_CONSTANT: 3}

    for subpkg_name in sorted(raw["subpackages"].keys()):
        subpkg = raw["subpackages"][subpkg_name]
        # Sort symbols: by kind group, then alphabetical
        symbols = sorted(
            subpkg["symbols"],
            key=lambda s: (GROUP_ORDER.get(s["kind"], 99), s["name"].lower()),
        )
        normalized["subpackages"][subpkg_name] = {
            "all": subpkg["all"],
            "symbols": symbols,
        }

    # Sort contracts alphabetically by namespace, then by model name
    for ns in sorted(raw["contracts"].keys()):
        models = sorted(raw["contracts"][ns], key=lambda m: m["name"].lower())
        normalized["contracts"][ns] = models

    print(json.dumps(normalized, indent=2))  # noqa: T201
    eprint("Normalize complete.")


# ---------------------------------------------------------------------------
# RENDER subcommand
# ---------------------------------------------------------------------------


def _load_purposes(yaml_path: str) -> dict[str, str]:
    """Load subpackage purposes from YAML (simple hand-rolled parser — avoids PyYAML dep)."""
    purposes: dict[str, str] = {}
    with open(yaml_path) as f:
        for line in f:
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            if ": " in line:
                key, _, value = line.partition(": ")
                key = key.strip().strip('"').strip("'")
                value = value.strip().strip('"').strip("'")
                purposes[key] = value
    return purposes


def cmd_render(normalized_json_path: str, purposes_yaml_path: str) -> None:
    """Render the markdown manifest from normalized JSON + purposes YAML, print to stdout."""
    eprint(f"Rendering from {normalized_json_path}...")
    with open(normalized_json_path) as f:
        data = json.load(f)

    purposes = _load_purposes(purposes_yaml_path)

    meta = data["meta"]
    subpackages = data["subpackages"]
    contracts = data["contracts"]

    lines: list[str] = []

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------
    lines.append("<!--")
    lines.append(
        "generated-by:  capability-manifest skill (.claude/skills/capability-manifest)"
    )
    lines.append(f"sdk-version:   {meta['sdk_version']}")
    lines.append(f"source-sha:    {meta['source_sha']}")
    lines.append(f"source-date:   {meta['source_date']}")
    lines.append("do-not-edit:   re-run the skill instead of hand-editing")
    lines.append("-->")
    lines.append("")
    lines.append("# Atlan Application SDK — Capability Manifest")
    lines.append("")
    lines.append(
        "> Canonical inventory of every public symbol exposed by `application_sdk`,"
    )
    lines.append("> plus every typed Input/Output contract.")
    lines.append(
        "> Read this before reading SDK source. Generated by `.claude/skills/capability-manifest`."
    )
    lines.append(">")
    lines.append(
        "> **To check if current:** `awk '/^source-sha:/{print $2}' docs/agents/sdk-capabilities.md`"
        " vs `git log -1 --format=%H -- application_sdk/`"
    )
    lines.append("")

    # -----------------------------------------------------------------------
    # Section 1 — Subpackage index
    # -----------------------------------------------------------------------
    lines.append("## Subpackage Index")
    lines.append("")
    lines.append("| Subpackage | Purpose | Exports |")
    lines.append("|---|---|---|")

    for subpkg_name in sorted(subpackages.keys()):
        full_name = "application_sdk." + subpkg_name
        purpose = purposes.get(
            subpkg_name, purposes.get(full_name, "_(no description)_")
        )
        n_exports = len(subpackages[subpkg_name]["all"])
        lines.append(f"| `{full_name}` | {purpose} | {n_exports} |")

    lines.append("")

    # -----------------------------------------------------------------------
    # Section 2 — Per-subpackage detail
    # -----------------------------------------------------------------------
    lines.append("## Subpackage Details")
    lines.append("")

    GROUP_LABELS = {
        KIND_CLASS: "Classes",
        KIND_DECORATOR: "Decorators",
        KIND_FUNCTION: "Functions",
        KIND_CONSTANT: "Constants and Enums",
    }
    GROUP_ORDER = [KIND_CLASS, KIND_DECORATOR, KIND_FUNCTION, KIND_CONSTANT]

    for subpkg_name in sorted(subpackages.keys()):
        full_name = "application_sdk." + subpkg_name
        purpose = purposes.get(subpkg_name, purposes.get(full_name, ""))
        symbols = subpackages[subpkg_name]["symbols"]

        lines.append(f"## `{full_name}`")
        lines.append("")
        if purpose:
            lines.append(purpose)
            lines.append("")

        # Group symbols by kind
        by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in GROUP_ORDER}
        for sym in symbols:
            k = sym["kind"]
            if k in by_kind:
                by_kind[k].append(sym)

        for kind in GROUP_ORDER:
            group_symbols = by_kind[kind]
            if not group_symbols:
                continue
            lines.append(f"### {GROUP_LABELS[kind]}")
            lines.append("")
            for sym in group_symbols:
                name = sym["name"]
                sig = sym["signature"]
                summary = sym["summary"]
                filepath = sym["filepath"]
                prefix = "@" if kind == KIND_DECORATOR else ""
                lines.append(f"#### `{prefix}{name}`")
                lines.append("")
                lines.append(f"- **Import:** `from {full_name} import {name}`")
                if sig:
                    lines.append(f"- **Signature:** `{sig}`")
                lines.append(f"- **Summary:** {summary}")
                if filepath:
                    lines.append(f"- **Defined in:** `{filepath}`")
                lines.append("")

    # -----------------------------------------------------------------------
    # Section 3 — Contracts
    # -----------------------------------------------------------------------
    lines.append("## Contracts")
    lines.append("")
    lines.append(
        "Strongly-typed Inputs/Outputs for SDK methods."
        " All inherit from `application_sdk.contracts.base.{Input, Output}` (Pydantic)."
    )
    lines.append("")

    for ns in sorted(contracts.keys()):
        models = contracts[ns]
        if not models:
            continue
        lines.append(f"### `{ns}`")
        lines.append("")
        for model in models:
            m_name = model["name"]
            m_summary = model["summary"]
            m_filepath = model["filepath"]
            m_fields = model["fields"]
            lines.append(f"#### `{m_name}`")
            lines.append("")
            lines.append(f"- **Import:** `from {ns} import {m_name}`")
            lines.append(f"- **Summary:** {m_summary}")
            if m_fields:
                lines.append("- **Fields:**")
                for field in m_fields:
                    f_name = field["name"]
                    f_ann = field["annotation"]
                    f_default = field["default"]
                    f_desc = field["description"]
                    field_str = f"  - `{f_name}: {f_ann}`"
                    if f_default is not None and f_default != "None":
                        field_str += f" `= {f_default}`"
                    if f_desc:
                        field_str += f" — {f_desc}"
                    lines.append(field_str)
            if m_filepath:
                lines.append(f"- **Defined in:** `{m_filepath}`")
            lines.append("")

    # Final newline
    content = "\n".join(lines) + "\n"
    print(content, end="")  # noqa: T201
    eprint("Render complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)  # noqa: T201
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "dump":
        cmd_dump()
    elif cmd == "normalize":
        if len(sys.argv) < 3:
            eprint("Usage: extractor.py normalize <raw.json>")
            sys.exit(1)
        cmd_normalize(sys.argv[2])
    elif cmd == "render":
        if len(sys.argv) < 4:
            eprint(
                "Usage: extractor.py render <normalized.json> <subpackage-purposes.yaml>"
            )
            sys.exit(1)
        cmd_render(sys.argv[2], sys.argv[3])
    else:
        eprint(f"Unknown subcommand: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
