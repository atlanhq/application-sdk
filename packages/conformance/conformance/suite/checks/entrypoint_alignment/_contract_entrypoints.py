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

    dag_workflow_types: frozenset[tuple[str, str | None]] = field(
        default_factory=frozenset
    )
    """Every ``workflow_type`` the DAG dispatches, paired with the node's
    ``app_name`` (``None`` when the node carries none). The check routes a
    manifest-declared ``legacy_workflow_types`` alias through a matching node
    only when the node's own identity does not contradict the declaration —
    a node naming a *different* app dispatches on that app's worker, so the
    declaration alone cannot make it reach this one."""

    own_app_names: frozenset[str] = field(default_factory=frozenset)
    """App names this manifest's own DAG claims — the ``<app>`` half of every
    colon-qualified ``workflow_type`` it routes.

    This is the manifest's own identity, taken from the same artifact that
    declares the aliases. A bare node whose ``app_name`` falls outside this set
    dispatches on another app's worker, so a local alias declaration cannot make
    it reach an entry point here. Deriving it from the manifest rather than from
    the App classes found in code matters in a repo that defines more than one
    App: a node naming the *other* app must not launder an alias belonging to
    this one. Empty in ``multi``/``absent`` modes.
    """

    legacy_aliases: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    """Inbound-only workflow type aliases the manifest declares, as
    ``(alias, target entry-point name)`` pairs (CONNECT-1081).

    The manifest is the contracted declaration site for these; K015 holds it in
    agreement with the SDK's ``App.legacy_workflow_types`` class attribute, and
    P016 routes off this rather than off the code declaration.  Empty in
    ``multi``/``absent`` modes, matching :attr:`routes` — P016 never consults
    routes outside single mode.  K015 reads the per-entry-point manifests of a
    multi-mode tree itself.
    """


@dataclass(frozen=True)
class LegacyAliasDeclaration:
    """The ``legacy_workflow_types`` block of one manifest."""

    aliases: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    """``(alias, target entry-point name)`` pairs."""

    removal_version: str = ""
    """Declared expiry, or ``""`` when the block omits ``removal_version``."""


def load_manifest_document(manifest_path: Path) -> dict[str, Any] | None:
    """Load a manifest as a JSON object, or ``None`` when it cannot be read.

    Unreadable, malformed, and non-object manifests all collapse to ``None``: a
    conformance check reports drift it can prove, and a manifest it cannot parse
    proves nothing.
    """
    try:
        data: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parse_legacy_aliases(data: dict[str, Any]) -> LegacyAliasDeclaration:
    """Read the ``legacy_workflow_types`` block out of a parsed manifest.

    A block whose entries are not ``{"alias": str, "entrypoint": str}`` is
    skipped entry by entry rather than failing the whole parse — a hand-edited
    manifest should surface as the drift it is, not as a silent empty read.
    """
    block = data.get("legacy_workflow_types")
    if not isinstance(block, dict):
        return LegacyAliasDeclaration()

    aliases: set[tuple[str, str]] = set()
    for entry in block.get("aliases") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("alias")
        target = entry.get("entrypoint")
        if isinstance(alias, str) and alias and isinstance(target, str) and target:
            aliases.add((alias, target))

    removal_version = block.get("removal_version")
    return LegacyAliasDeclaration(
        aliases=frozenset(aliases),
        removal_version=removal_version if isinstance(removal_version, str) else "",
    )


def _routes_from_dag(
    dag: Any,
) -> tuple[frozenset[str], frozenset[tuple[str, str | None]], frozenset[str]]:
    """Collect DAG-declared workflow types from a manifest's ``dag`` subtree.

    Returns ``(routes, dag_workflow_types, own_app_names)``:

    ``routes``
        Wire names taken from every ``workflow_type`` of the form
        ``"<app>:<wire-name>"`` (the part after the colon).

    ``dag_workflow_types``
        Every ``(workflow_type, node app_name-or-None)`` pair in the DAG.
        Bare types (e.g. ``"PublishWorkflow"``) are platform/other-app nodes
        unless the manifest declares them in ``legacy_workflow_types`` AND the
        node's ``app_name`` does not name a different app.

    ``own_app_names``
        The ``<app>`` half of those same colon-qualified types — the app
        identities this manifest's DAG routes to itself.

    The walk is scoped to the ``dag`` subtree, so a ``workflow_type`` appearing
    elsewhere in the manifest is not collected.  It does **not** pin the
    ``<app>`` prefix to this app's own name — the (rare) cross-app
    ``"<other>:<wire>"`` node inside the DAG would also contribute its wire
    name.  In a generated single-mode manifest every routed node is this app's
    own, so in practice this returns this app's DAG-routed entry points.
    """
    if dag is None:
        return frozenset(), frozenset(), frozenset()

    wire_names: set[str] = set()
    dag_types: set[tuple[str, str | None]] = set()
    app_names: set[str] = set()

    def _node_app_name(node: dict[str, Any]) -> str | None:
        inputs = node.get("inputs")
        source = inputs if isinstance(inputs, dict) else node
        app_name = source.get("app_name")
        return app_name if isinstance(app_name, str) and app_name else None

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            wt = node.get("workflow_type")
            if isinstance(wt, str) and wt:
                dag_types.add((wt, _node_app_name(node)))
                if ":" in wt:
                    prefix, wire = wt.split(":", 1)
                    wire_names.add(wire)
                    if prefix:
                        app_names.add(prefix)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(dag)
    return frozenset(wire_names), frozenset(dag_types), frozenset(app_names)


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
        data = load_manifest_document(single_manifest)
        if data is None:
            return ContractEntrypointScan(names=frozenset(), mode="single")
        routes, dag_workflow_types, own_app_names = _routes_from_dag(data.get("dag"))
        return ContractEntrypointScan(
            names=frozenset(),
            mode="single",
            routes=routes,
            dag_workflow_types=dag_workflow_types,
            own_app_names=own_app_names,
            legacy_aliases=parse_legacy_aliases(data).aliases,
        )

    # app/generated/ exists but contains no manifest.json anywhere — treat as absent
    # (e.g. a partially scaffolded repo that hasn't been generated yet).
    return ContractEntrypointScan(names=frozenset(), mode="absent")
