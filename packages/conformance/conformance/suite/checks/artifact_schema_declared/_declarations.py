"""Read the declared artifact-schema keys out of the committed generated tree.

``artifactSchemas`` is authored in the app's pkl contract and rendered by the
toolkit to ``artifact_schemas.json``, keyed by the name of the ``FileReference``
contract field each entry describes.  Like every other contract-side read in
this suite, K016 reads the *committed* generated artifact rather than the pkl
source: it avoids requiring the ``pkl`` CLI at check time and matches what the
runtime actually loads.  Freshness is C002's job.

Two layouts, searched nested-first — the same order the handler uses to locate
an entry point's form configmap (``handler/service.py``) and the same order the
SDK's registration-time guard uses, so all three readers of this file agree:

``app/generated/<wire-name>/artifact_schemas.json``
    A multi-entrypoint (bundle) app.  ``artifactSchemas`` is a per-entrypoint
    property, so each entry point's declarations land under its own wire-named
    subdirectory.

``app/generated/artifact_schemas.json``
    A single-entrypoint app, flat alongside ``manifest.json``.

The flat file is a safe fallback *by construction*: declaring
``artifactSchemas`` on a bundle root is a toolkit generation error, so a
root-level file can only ever belong to a single-entrypoint app.

**The fallback is between files, never between fields.**  The first file that
exists is the final answer.  Unioning the two files' keys would let one entry
point's boundary be satisfied by another scope's declarations — the silent
wrong answer the bundle-root generation error exists to prevent.

**Absent and unreadable are different answers.**  An absent file means the app
declares nothing, which is exactly the state this rule reports.  A file that is
present but malformed means *we cannot tell*, and reporting it as "declares
nothing" would turn one bad JSON blob into a finding on every boundary field.
A malformed generated artifact is C002/K005 territory; K016 stays quiet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ARTIFACT_SCHEMAS_FILENAME = "artifact_schemas.json"

DeclarationStatus = Literal["declared", "absent", "unreadable"]


@dataclass(frozen=True)
class Declarations:
    """What the generated tree says about one entry point's artifact schemas."""

    status: DeclarationStatus
    """``declared`` — a file was read; ``absent`` — no file exists at any
    candidate path; ``unreadable`` — a file exists but could not be understood."""

    keys: frozenset[str] = frozenset()
    """Declared contract field names.  Empty unless ``status`` is ``declared``."""

    path: Path | None = None
    """The file that answered, or the flat path when nothing existed (so a
    finding can name where the declaration belongs)."""

    @property
    def checkable(self) -> bool:
        """Whether a missing key here is a real finding rather than an unknown."""
        return self.status != "unreadable"


def candidate_paths(root: Path, mode: str, wire_name: str | None) -> list[Path]:
    """Return where *wire_name*'s declarations may live, nested-first.

    Args:
        root: Repo root.
        mode: The :func:`~conformance.suite.checks.entrypoint_alignment._contract_entrypoints.scan_contract`
            mode — ``"single"`` or ``"multi"``.
        wire_name: The entry point's kebab-case wire name; ``None`` for an
            implicit ``App.run()`` entry point, which only ever exists in a
            single-entrypoint app.

    Returns:
        Candidate paths in search order, or ``[]`` for a shape this rule does
        not understand.
    """
    generated = root / "app" / "generated"
    flat = generated / ARTIFACT_SCHEMAS_FILENAME
    if mode == "single":
        return [flat]
    if mode == "multi" and wire_name:
        return [generated / wire_name / ARTIFACT_SCHEMAS_FILENAME, flat]
    return []


def read_declarations(root: Path, mode: str, wire_name: str | None) -> Declarations:
    """Resolve one entry point's declarations from the committed generated tree."""
    candidates = candidate_paths(root, mode, wire_name)
    if not candidates:
        return Declarations(status="unreadable")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return Declarations(status="unreadable", path=path)
        schemas = data.get("schemas") if isinstance(data, dict) else None
        if not isinstance(schemas, dict):
            return Declarations(status="unreadable", path=path)
        return Declarations(
            status="declared",
            keys=frozenset(str(key) for key in schemas),
            path=path,
        )

    # Nothing exists. The app declares nothing, and the finding should point at
    # where the declaration would land — the first (most specific) candidate.
    return Declarations(status="absent", path=candidates[0])
