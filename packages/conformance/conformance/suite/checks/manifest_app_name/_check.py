"""K013 ManifestNodeAppNameMisattributed — check implementation.

Reads every committed generated ``manifest.json`` and flags a DAG node whose
declared ``app_name`` disagrees with the app that actually runs it.

``app_name`` is the identity a node's logs are *written* under and *read back*
by. A wrong value therefore does not merely mislabel the step — the logs become
unreachable ("No error logs available for this pod" in the Workflow Center even
though logging worked) and failures are attributed to the wrong app (CNCT-24,
CNCT-129).

Two independent signals establish which app owns a node, so a contract that gets
it wrong is caught whichever way it got there:

* **workflow_type** — Automation Engine hosts none of the toolkit-owned
  workflows, so ``QueryIntelligenceWorkflow`` + ``automation-engine`` is drift by
  construction: the contract hand-wrote a raw ``DAGNode`` instead of the matching
  node class and inherited the default.
* **task_queue** — a queue of the form ``atlan-<system-app>-…`` says which worker
  polls the node, and so which app runs it, regardless of workflow type.

The rule reports **log identity only**. It never asks for a ``task_queue``
change: the queue is the routing decision and is generally the correct thing in
these manifests — it is precisely because routing was right that the
misattribution went unnoticed.

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
# Automation Engine hosts none of these — each has its own worker — so pairing
# one with the AE default is always drift, never configuration.
_BUILTIN_WORKFLOW_APP_NAMES = {
    "QueryIntelligenceWorkflow": "query-intelligence",
    "PublishWorkflow": "publish",
    "LineageWorkflow": "lineage",
    "PopularityWorkflow": "popularity",
    "NotificationWorkflow": "notification-app",
}

# Apps whose task queues are recognisable by name. A queue of the form
# ``atlan-<app>-<suffix>`` for one of these says which worker polls the node,
# and therefore which app runs it — independent of ``workflow_type``.
#
# Restricted to this closed set on purpose: the suffix is not parseable in
# general (``-production`` vs ``-{deployment_name}`` vs a tenant name), so the
# check anchors on the *app* segment against known values rather than trying to
# interpret what follows it.
_SYSTEM_APP_QUEUE_OWNERS = frozenset(
    {
        "query-intelligence",
        "publish",
        "lineage",
        "popularity",
        "notification-app",
    }
)

# Anchor the app segment as the known-app alternation and accept any non-empty
# suffix. The suffix is not parseable in general (``-production`` vs
# ``-production-us-east-1`` vs ``-{deployment_name}`` vs a tenant name), so the
# app is matched against the closed set rather than split off the queue. A lazy
# ``.+?`` app group would mis-capture on a multi-segment suffix — e.g.
# ``atlan-query-intelligence-production-us-east-1`` would yield
# ``query-intelligence-production-us-east``, which is not a known app and so
# silently no-ops the task_queue signal on exactly the drift it exists to catch.
# Longest-first so a hyphenated app name (``notification-app``) is never
# shadowed by a prefix that is also a known app.
_QUEUE_RE = re.compile(
    r"^atlan-(?P<app>"
    + "|".join(sorted(_SYSTEM_APP_QUEUE_OWNERS, key=len, reverse=True))
    + r")-.+$"
)

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

    All three are consulted because older manifests do not carry all of them:
    ``inputs.args.app_name`` only appeared with CNCT-93, and a pre-CNCT-93
    manifest that stamped it *only* there would otherwise be a blind spot.
    """
    raw_inputs = node.get("inputs")
    inputs: dict[str, Any] = raw_inputs if isinstance(raw_inputs, dict) else {}
    raw_args = inputs.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    for candidate in (
        node.get("app_name"),
        inputs.get("app_name"),
        args.get("app_name"),
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


def _queue_owner(node: dict[str, Any]) -> str | None:
    """The system app whose queue this node dispatches to, if recognisable.

    Returns ``None`` for a connector's own queue or any queue whose app segment
    is not a known system app — those say nothing about which app should own the
    node's log identity.
    """
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return None
    task_queue = inputs.get("task_queue")
    if not isinstance(task_queue, str):
        return None
    match = _QUEUE_RE.match(task_queue)
    if match is None:
        return None
    app = match.group("app")
    return app if app in _SYSTEM_APP_QUEUE_OWNERS else None


def _expected_app(node: dict[str, Any], declared: str) -> tuple[str, str] | None:
    """Which app should own this node, and the evidence for it.

    Two independent signals, checked in order of strength:

    1. **workflow_type** — a toolkit-owned workflow names its app exactly. Only
       reported when the node still carries the raw ``DAGNode`` default, since
       an author who set some *other* value may be routing to a bespoke worker
       and that is their call to make.
    2. **task_queue** — a queue of the form ``atlan-<system-app>-…`` says which
       worker polls the node, so a disagreeing ``app_name`` is misattributed
       whatever the workflow type is. This catches a node whose workflow type
       the toolkit does not own but whose queue is unambiguous.

    Returns ``(expected_app, evidence)``, or ``None`` when the node is
    consistent or nothing can be concluded about it.
    """
    workflow_type = _node_workflow_type(node)
    if (
        workflow_type in _BUILTIN_WORKFLOW_APP_NAMES
        and declared == _AE_DEFAULT_APP_NAME
    ):
        expected = _BUILTIN_WORKFLOW_APP_NAMES[workflow_type]
        return expected, f"runs '{workflow_type}'"

    owner = _queue_owner(node)
    if owner is not None and owner != declared:
        inputs = node.get("inputs")
        task_queue = inputs.get("task_queue") if isinstance(inputs, dict) else ""
        return owner, f"dispatches to '{task_queue}'"

    return None


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
            declared = _node_app_name(node)
            if declared is None:
                continue

            verdict = _expected_app(node, declared)
            if verdict is None:
                continue
            expected, evidence = verdict

            workflow_type = _node_workflow_type(node)
            node_class = _BUILTIN_NODE_CLASSES.get(workflow_type or "")
            remedy = (
                f"Replace it with the built-in '{node_class}' in "
                f"'contract/app.pkl' (which sets appName and taskQueue together), "
                f'or set appName = "{expected}" on the node'
                if node_class is not None
                else f"Set appName = \"{expected}\" on the node in 'contract/app.pkl'"
            )
            findings.append(
                Finding(
                    rule_id=_RULE_ID,
                    file=rel,
                    line=_app_name_line(text, node_id),
                    column=1,
                    message=(
                        f"DAG node '{node_id}' in '{rel}' {evidence}, so "
                        f"'{expected}' runs it — but it declares app_name "
                        f"'{declared}'. app_name is the identity this node's logs "
                        f"are written under and read back by, so the mismatch does "
                        f"not merely mislabel the step: its logs become unreachable "
                        f"in the Workflow Center ('No error logs available for this "
                        f"pod') and its failures are attributed to the wrong app. "
                        f"{remedy}, then regenerate with 'pkl eval -m . "
                        f"contract/app.pkl'. Change app_name only — task_queue is "
                        f"the routing decision and is already correct. Never "
                        f"hand-edit the generated manifest: it is a pkl eval "
                        f"output, and being JSON it carries no comment syntax to "
                        f"suppress this finding on."
                    ),
                    snippet=None,
                    suppressed=False,
                    suppression_justification=None,
                )
            )

    return findings
