"""K013 ManifestNodeAppNameMisattributed — check implementation.

Reads every committed generated ``manifest.json`` and flags a DAG node that
declares a toolkit-owned ``workflow_type`` while still carrying the raw
``DAGNode`` ``app_name`` default, ``"automation-engine"``.

Automation Engine never runs these workflows itself — ``QueryIntelligenceWorkflow``
runs on the QI worker, ``PublishWorkflow`` on publish-app, and so on. So the
pairing is never legitimate: it means the contract hand-wrote a ``DAGNode``
instead of using the matching built-in node class (``QueryIntelligenceNode``,
``PublishNode``, …), inherited the AE default, and now files that node's logs,
metrics and failures under ``automation-engine`` rather than the app that
actually ran them (CNCT-24).

Because the manifest is a ``pkl eval`` output and JSON carries no comment
syntax, there is nowhere to write a suppression directive — as with K009, the
only resolution is to fix ``contract/app.pkl`` and regenerate. Findings anchor
on the offending node's ``"app_name"`` line purely for navigation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from conformance.suite.schema.findings import Finding

_RULE_ID = "K013"

_AE_DEFAULT_APP_NAME = "automation-engine"

# Workflow types the contract-toolkit owns, mapped to the app that runs them.
# Mirrors ``builtinWorkflowAppNames`` in ``contract-toolkit/src/App.pkl``; the
# toolkit resolves this at render time, so a manifest still pairing one of these
# with the AE default was generated before that fix (or by a hand-edit).
_BUILTIN_WORKFLOW_APP_NAMES = {
    "QueryIntelligenceWorkflow": "query-intelligence",
    "PublishWorkflow": "publish",
    "LineageWorkflow": "lineage",
    "PopularityWorkflow": "popularity",
    "NotificationWorkflow": "notification-app",
}

# The built-in node class an author should use instead of a raw ``DAGNode``.
_BUILTIN_NODE_CLASSES = {
    "QueryIntelligenceWorkflow": "QueryIntelligenceNode",
    "PublishWorkflow": "PublishNode",
    "LineageWorkflow": "LineageNode",
    "PopularityWorkflow": "PopularityNode",
    "NotificationWorkflow": "NotificationNode",
}

# Both generated roots are scanned: ``app/generated/`` is the canonical location,
# and some apps additionally commit ``contract/generated/`` (the pkl output dir
# before it is copied), so a violation is reported wherever it is committed.
_GENERATED_ROOTS = ("app/generated", "contract/generated")


def _manifest_paths(root: Path) -> list[Path]:
    """Every committed ``manifest.json`` under the generated roots, sorted."""
    paths: list[Path] = []
    for rel in _GENERATED_ROOTS:
        base = root / rel
        if base.is_dir():
            paths.extend(p for p in base.rglob("manifest.json") if p.is_file())
    return sorted(set(paths))


def _node_app_name(node: dict[str, Any]) -> str | None:
    """The node's declared ``app_name``, preferring the top-level key.

    The toolkit renders the same value at three positions (top-level,
    ``inputs.app_name``, ``inputs.args.app_name``); K013 reports on the node's
    identity, so any position carrying the AE default counts. Cross-position
    disagreement within one node is a different concern and not this rule's.
    """
    for candidate in (
        node.get("app_name"),
        (node.get("inputs") or {}).get("app_name")
        if isinstance(node.get("inputs"), dict)
        else None,
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _node_workflow_type(node: dict[str, Any]) -> str | None:
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return None
    workflow_type = inputs.get("workflow_type")
    return workflow_type if isinstance(workflow_type, str) else None


def _app_name_line(text: str, node_id: str) -> int:
    """Best-effort line number of the offending node's first ``app_name`` key.

    Anchors the finding inside the right node by scanning forward from the
    node's own key. Falls back to line 1 rather than guessing wrong — the
    manifest is generated, so the line is navigational only.
    """
    lines = text.splitlines()
    node_key = re.compile(rf'^\s*"{re.escape(node_id)}"\s*:')
    app_name_key = re.compile(r'^\s*"app_name"\s*:')
    start = next(
        (i for i, line in enumerate(lines) if node_key.match(line)),
        None,
    )
    if start is None:
        return 1
    for i in range(start, len(lines)):
        if app_name_key.match(lines[i]):
            return i + 1
    return start + 1


def scan_all(paths: list[Path], root: Path) -> list[Finding]:  # noqa: ARG001
    """Flag toolkit-owned DAG nodes still attributed to ``automation-engine``.

    No-ops when no generated manifest exists or a manifest is unreadable /
    malformed / has no ``dag`` object — conservative, mirroring K006: a repo
    shape this check does not understand yields a false negative, not a false
    positive.
    """
    findings: list[Finding] = []

    for path in _manifest_paths(root):
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        dag = data.get("dag")
        if not isinstance(dag, dict):
            continue

        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        for node_id, node in dag.items():
            if not isinstance(node, dict):
                continue
            workflow_type = _node_workflow_type(node)
            if workflow_type not in _BUILTIN_WORKFLOW_APP_NAMES:
                continue
            if _node_app_name(node) != _AE_DEFAULT_APP_NAME:
                continue

            expected = _BUILTIN_WORKFLOW_APP_NAMES[workflow_type]
            node_class = _BUILTIN_NODE_CLASSES[workflow_type]
            findings.append(
                Finding(
                    rule_id=_RULE_ID,
                    file=rel,
                    line=_app_name_line(text, node_id),
                    column=1,
                    message=(
                        f"DAG node '{node_id}' in '{rel}' runs "
                        f"'{workflow_type}' but declares "
                        f"app_name '{_AE_DEFAULT_APP_NAME}'. Automation Engine does "
                        f"not run that workflow — '{expected}' does — so this node's "
                        f"logs, metrics and failures are filed under Automation "
                        f"Engine instead of the app that ran them, and the tenant's "
                        f"Workflow Center shows no logs for the step. The node was "
                        f"hand-written as a raw 'DAGNode', inheriting its "
                        f"'{_AE_DEFAULT_APP_NAME}' default. Replace it with the "
                        f"built-in '{node_class}' in 'contract/app.pkl' (which sets "
                        f"both appName and taskQueue), or set "
                        f"appName = \"{expected}\" on the node, then regenerate with "
                        f"'pkl eval -m . contract/app.pkl'. Upgrading "
                        f"app-contract-toolkit also corrects a defaulted appName at "
                        f"render time. Never hand-edit the generated manifest — "
                        f"it is a pkl eval output, and being JSON it carries no "
                        f"comment syntax to suppress this finding on. Fix the "
                        f"contract and regenerate."
                    ),
                    snippet=None,
                    suppressed=False,
                    suppression_justification=None,
                )
            )

    return findings
