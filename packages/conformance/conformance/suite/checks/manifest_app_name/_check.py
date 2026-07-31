"""K013 / K014 — per-node ``app_name`` consistency inside generated manifests.

The CNCT-129 fix makes the SDK stamp each DAG node's own ``app_name`` onto its
logs and metrics, resolved from that node's ``inputs.args.app_name`` (the value
the contract toolkit now emits). The tenant UI / Heracles query a node's logs by
its ``app_name`` (an equality filter on the lakehouse ``app_name`` column). Two
manifest-shaped invariants keep that path correct; both are checked here off the
committed generated DAG — ``app/generated/**/manifest.json`` or, for apps that
generate under ``contract/generated/`` (e.g. powerbi), that root:

* **K013 (block) — internal consistency.** Within a node, the three places an
  ``app_name`` can appear — the node's top-level ``app_name``, ``inputs.app_name``
  and ``inputs.args.app_name`` — must agree. If ``inputs.args.app_name`` (what the
  SDK stamps) diverges from the node's ``app_name`` (what the UI queries), the
  node's logs are stamped one value and queried under another: they vanish from
  the panel. This is exactly the CNCT-129 failure, one static diff away.
  No-op guard: a node with **no** ``inputs.args.app_name`` is never flagged —
  apps not yet regenerated onto the new toolkit carry no args key and fall back
  to the ``ATLAN_APPLICATION_NAME`` env value at runtime, which is correct.

* **K014 (warn) — cross-node distinctness, per manifest.** Two *different* DAG
  nodes in the *same* manifest must not share an ``app_name``: the UI keys logs
  on ``(correlation_id, app_name)`` with no per-node discriminator, so two
  co-running nodes with the same ``app_name`` overlap in the panel. Scoped per
  manifest on purpose — a bundle's ``crawler`` and ``miner`` manifests are
  separate runs, so the ``publish`` node's ``app_name`` recurring across them is
  fine and never compared.

Both no-op cleanly when neither generated root exists (SDK / library repos), when
a manifest has no parseable ``dag``, or when a node carries no ``app_name`` at
all.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from conformance.suite.schema.findings import Finding

_MISMATCH_RULE_ID = "K013"
_COLLISION_RULE_ID = "K014"


def _str_or_none(value: Any) -> str | None:
    """Return *value* iff it is a non-empty ``str``, else ``None``."""
    return value if isinstance(value, str) and value else None


def _node_app_names(
    node_data: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Return ``(top, inputs_app, args_app)`` string values present on a node."""
    top = _str_or_none(node_data.get("app_name"))
    inputs = node_data.get("inputs")
    if not isinstance(inputs, dict):
        return top, None, None
    inputs_app = _str_or_none(inputs.get("app_name"))
    args = inputs.get("args")
    args_app = _str_or_none(args.get("app_name")) if isinstance(args, dict) else None
    return top, inputs_app, args_app


def _scan_manifest(path: Path, root: Path) -> list[Finding]:
    """Emit K013/K014 findings for a single ``manifest.json``.

    Conservative on shape: a missing/unreadable/malformed file, or one with no
    ``dag`` object, yields no findings (mirrors K006's tolerance of a
    partially-generated manifest).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    dag = data.get("dag")
    if not isinstance(dag, dict):
        return []

    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    findings: list[Finding] = []
    # node id -> its declared (queried) app_name, for the per-manifest collision pass.
    top_by_node: dict[str, str] = {}

    for node_id, node_data in dag.items():
        if not isinstance(node_data, dict):
            continue
        top, inputs_app, args_app = _node_app_names(node_data)

        if top is not None:
            top_by_node[str(node_id)] = top

        # K013: only fires once the SDK-stamped args value is present AND it
        # disagrees with another present value for the same node.
        if args_app is not None:
            present = [v for v in (top, inputs_app, args_app) if v is not None]
            if len(set(present)) > 1:
                findings.append(
                    Finding(
                        rule_id=_MISMATCH_RULE_ID,
                        file=rel,
                        line=1,
                        column=1,
                        message=(
                            f"DAG node '{node_id}' has inconsistent app_name: "
                            f"inputs.args.app_name='{args_app}'"
                            + (f", app_name='{top}'" if top is not None else "")
                            + (
                                f", inputs.app_name='{inputs_app}'"
                                if inputs_app is not None
                                else ""
                            )
                            + ". The SDK stamps logs/metrics from "
                            "inputs.args.app_name, while the tenant UI / Heracles "
                            "query the node's app_name — when they differ the "
                            "node's logs are stamped one value and queried under "
                            "another, so they never appear (CNCT-129). All three "
                            "must be equal. Do not hand-edit the generated "
                            "manifest: fix the node's appName in contract/app.pkl "
                            "and re-run pkl eval so every app_name for the node "
                            "agrees."
                        ),
                    )
                )

    # K014: within THIS manifest, no two distinct nodes may share an app_name.
    nodes_by_app: dict[str, list[str]] = defaultdict(list)
    for node_id, app in top_by_node.items():
        nodes_by_app[app].append(node_id)
    for app, node_ids in nodes_by_app.items():
        if len(node_ids) > 1:
            joined = ", ".join(sorted(node_ids))
            findings.append(
                Finding(
                    rule_id=_COLLISION_RULE_ID,
                    file=rel,
                    line=1,
                    column=1,
                    message=(
                        f"DAG nodes [{joined}] all declare app_name='{app}' in the "
                        "same manifest. The tenant UI queries logs by "
                        "(correlation_id, app_name) with no per-node discriminator, "
                        "so these nodes' logs overlap in the panel and cannot be "
                        "told apart. Give each node a distinct app_name (set a "
                        "distinct appName per node in contract/app.pkl). If two "
                        "nodes genuinely run as the same app, suppress this warning "
                        "with a documented reason."
                    ),
                )
            )

    return findings


def scan_all(paths: list[Path], root: Path) -> list[Finding]:  # noqa: ARG001
    """Scan every committed generated ``manifest.json`` for app_name drift.

    Apps emit their generated manifests to one of two roots depending on how
    ``poe generate`` is wired: most use ``app/generated/`` (mysql, metabase,
    openapi, hightouch), but some — including powerbi, the app that motivated
    these rules — use ``contract/generated/`` (its task is
    ``cd contract && pkl eval -m generated app.pkl``). Both roots are scanned so
    the rules never silently skip an app because of where it generates.

    No-ops only when NEITHER root exists (SDK / library repos). Reads the
    generated manifests directly (not the Python source in ``paths``), so the
    per-file ``paths`` argument is unused. Relative-path reporting is anchored at
    ``root`` regardless of which generated root a manifest came from.
    """
    findings: list[Finding] = []
    seen: set[Path] = set()
    for generated in (root / "app" / "generated", root / "contract" / "generated"):
        if not generated.is_dir():
            continue
        for manifest_path in sorted(generated.glob("**/manifest.json")):
            resolved = manifest_path.resolve()
            if resolved in seen:  # defensive: same file reachable via both roots
                continue
            seen.add(resolved)
            findings.extend(_scan_manifest(manifest_path, root))
    return findings
