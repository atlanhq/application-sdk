"""Read contract entry-point names from the committed ``app/generated/`` tree.

The authoritative contract-side source is the *committed* ``app/generated/``
artifact tree, not the Pkl source.  This avoids requiring the ``pkl`` CLI at
conformance-check time and matches what the runtime actually serves.

Three modes are derived from the tree shape:

``absent``
    No ``app/generated/`` directory exists under the repo root.  The repo is
    not a native-app-contract repo; the P016 check is a no-op.

``multi``
    ``app/generated/`` contains one or more subdirectories each holding a
    ``manifest.json`` file.  Each subdir name is a contract entry-point name.

``single``
    ``app/generated/manifest.json`` exists at the root of the generated dir,
    and there are no per-entry-point subdirs.  The single entry point is served
    as the implicit default; its *name* is decoupled from the app name and is
    not constrained by P016.

Design note
-----------
The freshness of ``app/generated/`` is guaranteed by the C002
``bootstrap_drift`` check, which ensures that ``pkl eval`` is re-run whenever
``contract/app.pkl`` changes.  P016 therefore trusts that ``app/generated/``
reflects the current contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class ContractEntrypointScan:
    """Result of inspecting the ``app/generated/`` directory tree."""

    names: frozenset[str]
    """Contract entry-point names (subdir names that contain ``manifest.json``).

    Empty for ``single`` and ``absent`` modes.
    """

    mode: Literal["multi", "single", "absent"]
    """How the generated tree is structured."""

    routes: frozenset[str] = field(default_factory=frozenset)
    """Entry-point wire names declared as DAG **routes** in the single-mode
    ``manifest.json`` — i.e. any ``workflow_type`` of the form
    ``"<app>:<wire-name>"`` in the DAG.  A route-declared secondary entry point
    (BLDX-1342 route/card split) is a first-class shape: one marketplace card
    plus additional ``@entrypoint``\\ s the DAG invokes by ``workflow_type``,
    without per-entry-point bundle subdirs.  Empty in ``multi``/``absent`` modes
    and for older single-mode manifests that predate the ``<app>:<wire>``
    ``workflow_type`` convention.
    """


def _routes_from_manifest(manifest_path: Path) -> frozenset[str]:
    """Collect entry-point wire names declared as DAG routes in a manifest.

    Walks the manifest JSON and returns the wire name of every
    ``workflow_type`` of the form ``"<app>:<wire-name>"`` (the part after the
    colon).  Platform/other-app nodes without the ``<app>:`` convention (e.g.
    ``"PublishWorkflow"``) carry no colon and are ignored, so this yields only
    this app's own DAG-routed entry points.
    """
    try:
        data: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()

    wire_names: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            wt = node.get("workflow_type")
            if isinstance(wt, str) and ":" in wt:
                wire_names.add(wt.split(":", 1)[1])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return frozenset(wire_names)


def scan_contract(root: Path) -> ContractEntrypointScan:
    """Derive contract entry-point names from the committed ``app/generated/`` tree.

    Parameters
    ----------
    root:
        The repo root directory (the runner passes this as the ``--root``
        argument; tests use ``tmp_path``).

    Returns
    -------
    :class:`ContractEntrypointScan`
        Populated according to the three-mode logic described in the module
        docstring.
    """
    generated = root / "app" / "generated"

    if not generated.is_dir():
        return ContractEntrypointScan(names=frozenset(), mode="absent")

    # Multi-EP: immediate subdirs that each contain a manifest.json
    ep_names: set[str] = set()
    for child in generated.iterdir():
        if child.is_dir() and (child / "manifest.json").is_file():
            ep_names.add(child.name)

    if ep_names:
        return ContractEntrypointScan(names=frozenset(ep_names), mode="multi")

    # Single-EP: a manifest.json at the root of app/generated/
    single_manifest = generated / "manifest.json"
    if single_manifest.is_file():
        return ContractEntrypointScan(
            names=frozenset(),
            mode="single",
            routes=_routes_from_manifest(single_manifest),
        )

    # app/generated/ exists but contains no manifest.json anywhere — treat as absent
    # (e.g. a partially scaffolded repo that hasn't been generated yet).
    return ContractEntrypointScan(names=frozenset(), mode="absent")
