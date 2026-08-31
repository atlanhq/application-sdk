"""Read the ``extract`` node's ``inputs.args`` keys out of a generated manifest.

Companion to :mod:`._manifest_refs`, which reads the *output* side
(``$.<node>.outputs.<field>`` JSONPaths). This module reads the *input* side:
the arg keys the Automation Engine will send to the entrypoint, and the
``{{form-key}}`` placeholders they are wired to.

**Both nesting depths are load-bearing.** contract-toolkit 0.9.0 ("default
native manifest args to top-level") moved args out of the ``args.metadata{}``
envelope into flat top-level slots, but the fleet did not migrate in one step —
measured 2026-08-31, athena is fully flat (14/0), tableau is mostly nested
(6/12), salesforce is mixed (5/4). A reader that assumes either shape is wrong
on a large part of the fleet, so ``collect_arg_keys`` returns the union and
records which depth each key came from.

Arg keys are snake_case at *both* depths; only the placeholder is kebab-case
(``include_filter = "{{include-filter}}"``). So no case translation is needed
when comparing an arg key against a Python contract field name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PLACEHOLDER_RE = re.compile(r"^\{\{([^{}]+)\}\}$")

_JSONPATH_PREFIX = "$."

_EXTRACT_NODE_ID = "extract"

_METADATA_KEY = "metadata"


@dataclass(frozen=True)
class ManifestArg:
    """One argument slot on the ``extract`` node."""

    key: str
    """The arg key, always snake_case (e.g. ``include_filter``)."""

    nested: bool
    """True when the key sits under ``args.metadata``, False when top-level."""

    form_key: str | None
    """The ``{{...}}`` form key this slot is wired to (kebab-case), or ``None``
    when the value is a literal rather than a placeholder."""


@dataclass(frozen=True)
class ManifestArgs:
    """The ``extract`` node's argument surface for one ``manifest.json``."""

    manifest_path: str
    """Repo-relative path, e.g. ``app/generated/<entrypoint>/manifest.json``."""

    args: list[ManifestArg] = field(default_factory=list)

    def keys(self) -> set[str]:
        """Every arg key, at either depth."""
        return {a.key for a in self.args}

    def form_keys(self) -> set[str]:
        """Every ``{{form-key}}`` referenced by an arg slot."""
        return {a.form_key for a in self.args if a.form_key is not None}


def _form_key_of(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = _PLACEHOLDER_RE.match(value.strip())
    return m.group(1).strip() if m else None


def _is_cross_node_ref(value: Any) -> bool:
    """True for a ``$.<node>.outputs.<field>`` wiring value.

    Such a slot is filled by the platform from another node's runtime output,
    never by the caller, so it is not part of the user-config surface either
    rule reasons about.
    """
    return isinstance(value, str) and value.startswith(_JSONPATH_PREFIX)


def collect_arg_keys(path: Path, root: Path) -> ManifestArgs | None:
    """Parse *path* and return the ``extract`` node's argument surface.

    Returns ``None`` when the file is absent, unreadable, malformed JSON, or
    carries no ``dag.extract.inputs.args`` object — all treated as "nothing to
    check". Mirrors :func:`._manifest_refs.read_manifest`'s conservative
    posture: these are WARN rules and a false negative on an unexpected
    manifest shape beats a false positive.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    dag = data.get("dag")
    if not isinstance(dag, dict):
        return None
    node = dag.get(_EXTRACT_NODE_ID)
    if not isinstance(node, dict):
        return None
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return None
    args = inputs.get("args")
    if not isinstance(args, dict):
        return None

    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    collected: list[ManifestArg] = []

    for key, value in args.items():
        if key == _METADATA_KEY and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if _is_cross_node_ref(nested_value):
                    continue
                collected.append(
                    ManifestArg(
                        key=nested_key,
                        nested=True,
                        form_key=_form_key_of(nested_value),
                    )
                )
            continue
        if _is_cross_node_ref(value):
            continue
        collected.append(
            ManifestArg(key=key, nested=False, form_key=_form_key_of(value))
        )

    return ManifestArgs(manifest_path=rel, args=collected)
