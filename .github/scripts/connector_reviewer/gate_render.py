"""Compare a connector app's bootstrap-managed gate files with the render (FND-5325).

Not part of the engine, and never imported by it: the engine is standard
library only, and this file imports the conformance package. The `gate-fact`
step (`gate.make_fact`) runs it with the interpreter of an isolated venv that
holds exactly the `atlan-application-sdk-conformance` version the `uv.lock` of
the base the pull request merges into pins (the head's own pin is never read):

    <venv>/bin/python -I gate_render.py --root <head checkout> --out <json>

`-I` keeps this file's own directory off `sys.path` (it holds modules named
`select` and `trace`) and ignores `PYTHON*` variables. The working directory is
outside the checkout.

The comparison is the C002 drift check of that version, called as it is:
`bootstrap_drift.discover` names the files, `bootstrap_drift.scan_path` decides
each one. So the per-repo values it reads back off the file, the action pins it
ignores, an absent managed file and a retired one still present are all judged
exactly as the app's own Conformance gate judges them, and an app on an older
version is judged by that version. Two of C002's files are left out: the
write-if-absent scaffolds `tests.yaml` and `renovate.json`, which the app owns.

The canonical text for the diff is what `scan_path` rendered: `render` is
imported by name into `bootstrap_drift`, so it is wrapped there for the call.
Both sides are compared after `strip_action_pins`, as C002 compares them.

The checkout is the pull request head, read as data. A gate file reached
through a symlink is not read: its diff would go into a PR comment, and a
symlink could point at a file on the runner.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import importlib.metadata
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = 1
PACKAGE = "atlan-application-sdk-conformance"
# C002 files the app owns: bootstrap writes them once and never again.
SCAFFOLDS = frozenset({".github/workflows/tests.yaml", "renovate.json"})
DIFF_LINES = 24  # diff lines kept per file
DIFF_CHARS = 1500  # and characters
MAX_BYTES = 512_000  # a managed file larger than this is not read


def _linked(root: Path, rel: str) -> bool:
    """Whether `rel`, or a directory on the way to it, is a symlink."""
    here = root
    for part in Path(rel).parts:
        here = here / part
        if here.is_symlink():
            return True
    return False


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def summarise(canonical: str, head: str, strip: Callable[[str], str]) -> dict[str, Any]:
    """The diff from the render to the head, pins stripped: counts and the first lines."""
    lines = list(
        difflib.unified_diff(
            strip(canonical).splitlines(),
            strip(head).splitlines(),
            "bootstrap render",
            "pull request",
            n=1,
            lineterm="",
        )
    )
    body = [ln for ln in lines[2:]]
    added = sum(1 for ln in body if ln.startswith("+"))
    removed = sum(1 for ln in body if ln.startswith("-"))
    shown: list[str] = []
    used = 0
    for ln in body[:DIFF_LINES]:
        if used + len(ln) + 1 > DIFF_CHARS:
            break
        shown.append(ln)
        used += len(ln) + 1
    return {
        "added": added,
        "removed": removed,
        "diff": "\n".join(shown),
        "cut": len(shown) < len(body),
    }


def compare(root: Path) -> dict[str, Any]:
    """The fact for the checkout at `root`. Raises ImportError or
    AttributeError when the installed version lacks what this reads."""
    # Typed Any: the attributes are those of whichever version is installed.
    drift: Any = importlib.import_module("conformance.suite.checks.bootstrap_drift")
    render_mod: Any = importlib.import_module("conformance.bootstrap.render")
    try:
        extract: Any = importlib.import_module("conformance.bootstrap.extract")
        strip: Callable[[str], str] = extract.strip_action_pins
    except (ImportError, AttributeError):
        strip = drift.strip_action_pins
    retired = set(getattr(render_mod, "RETIRED_FILES", ()))
    retired |= {
        f".github/workflows/{n}" for n in getattr(render_mod, "RETIRED_WORKFLOWS", ())
    }
    retired |= set(getattr(render_mod, "RETIRED_ACTION_FILES", ()))

    paths: list[str] = []
    for p in drift.discover(root):
        rel = Path(p).relative_to(root).as_posix()
        if rel not in SCAFFOLDS and rel not in paths:
            paths.append(rel)

    rendered: list[str] = []
    real_render = drift.render

    def recording(*args: Any, **kwargs: Any) -> str:
        out = real_render(*args, **kwargs)
        rendered.append(out)
        return out

    mismatches: dict[str, dict[str, Any]] = {}
    unchecked: dict[str, str] = {}
    drift.render = recording
    try:
        for rel in paths:
            if _linked(root, rel):
                unchecked[rel] = "a symlink: not read"
                continue
            path = root / rel
            if path.exists() and _read(path) is None:
                unchecked[rel] = "not a readable text file"
                continue
            rendered.clear()
            try:
                found = drift.scan_path(path, root)
            except Exception as e:  # noqa: BLE001 - one file never stops the others
                unchecked[rel] = f"the check failed ({type(e).__name__})"
                continue
            if not found:
                continue
            if rel in retired:
                mismatches[rel] = {"kind": "retired", "added": 0, "removed": 0}
            elif not path.exists():
                canonical = rendered[-1] if rendered else ""
                mismatches[rel] = {
                    "kind": "absent",
                    "added": 0,
                    "removed": len(canonical.splitlines()),
                }
            else:
                head = _read(path) or ""
                entry: dict[str, Any] = {"kind": "drifted"}
                if rendered:
                    entry.update(summarise(rendered[-1], head, strip))
                mismatches[rel] = entry
    finally:
        drift.render = real_render
    return {
        "schema": SCHEMA,
        "status": "ok",
        "conformance": importlib.metadata.version(PACKAGE),
        "gate_files": paths,
        "mismatches": mismatches,
        "unchecked": unchecked,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gate_render")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if (root / "renovate.json").is_symlink():
        # C002 reads it for conformance.yaml's exit-zero fallback.
        fact: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason": "renovate.json is a symlink at the pull request head",
        }
    else:
        try:
            fact = compare(root)
        except (ImportError, AttributeError, TypeError) as e:
            fact = {
                "schema": SCHEMA,
                "status": "unavailable",
                "reason": f"this conformance version cannot be compared ({type(e).__name__})",
            }
    Path(args.out).write_text(json.dumps(fact), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
