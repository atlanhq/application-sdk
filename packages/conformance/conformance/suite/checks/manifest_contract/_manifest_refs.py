"""Read ``app/generated/**/manifest.json`` and extract ``$.<node>.outputs.<field>`` refs.

The Automation Engine DAG the contract-toolkit emits wires a downstream node's
args to an upstream node's runtime output via a JSONPath string such as
``$.extract.outputs.publish_state_prefix`` (see ``PublishNode`` /
``generateDAG()`` in ``contract-toolkit/src/App.pkl``). These strings appear
anywhere inside a node's ``inputs.args`` value (which may itself be a nested
dict, e.g. the ``notifications`` node's ``metadata`` block).

The entrypoint's own DAG node id is always the literal string ``"extract"``
— confirmed in ``App.pkl``'s ``generateDAG()`` (its own doc comment: "the node
id is always `extract`") and empirically in every example manifest, including
the multi-entrypoint ``bundle`` example's two per-entrypoint manifests. So a
reference is in K006's scope iff its node is ``"extract"`` — no app_name or
contract-name lookup is needed to identify "my own" node.

``$.workflow.*`` / ``$.failure.*`` references (AE run/failure context injected
into the ``notifications`` node) never match ``outputs`` and are naturally
excluded by the regex.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REF_RE = re.compile(r"^\$\.(?P<node>[\w-]+)\.outputs\.(?P<field>\w+)$")

_EXTRACT_NODE_ID = "extract"


@dataclass(frozen=True)
class ManifestRef:
    """A single ``$.<node>.outputs.<field>`` reference found in a manifest."""

    node: str
    field: str


@dataclass(frozen=True)
class ManifestDag:
    """Parsed ``dag`` block of one ``manifest.json``."""

    manifest_path: str
    """Repo-relative path, e.g. ``app/generated/manifest.json`` or
    ``app/generated/<entrypoint>/manifest.json``."""

    refs: list[ManifestRef] = field(default_factory=list)

    def own_refs(self) -> Iterator[ManifestRef]:
        """Yield references whose node is the entrypoint's own (``"extract"``) node."""
        for ref in self.refs:
            if ref.node == _EXTRACT_NODE_ID:
                yield ref


def _iter_strings(value: Any) -> Iterator[str]:
    """Recursively yield every string leaf inside a JSON value (dict/list/str)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _extract_refs(dag: dict[str, Any]) -> list[ManifestRef]:
    refs: list[ManifestRef] = []
    for node_data in dag.values():
        if not isinstance(node_data, dict):
            continue
        inputs = node_data.get("inputs")
        if not isinstance(inputs, dict):
            continue
        args = inputs.get("args")
        if args is None:
            continue
        for s in _iter_strings(args):
            m = _REF_RE.match(s)
            if m:
                refs.append(ManifestRef(node=m.group("node"), field=m.group("field")))
    return refs


def read_manifest(path: Path, root: Path) -> ManifestDag | None:
    """Parse *path* and extract its ``$.<node>.outputs.<field>`` references.

    Returns ``None`` when the file is absent, unreadable, malformed JSON, or
    has no ``dag`` object — all treated as "nothing to check" (conservative;
    K006 is WARN and prefers a false negative over a false positive on a
    partially-generated or unexpected manifest shape).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dag = data.get("dag")
    if not isinstance(dag, dict):
        return None

    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    return ManifestDag(manifest_path=rel, refs=_extract_refs(dag))
