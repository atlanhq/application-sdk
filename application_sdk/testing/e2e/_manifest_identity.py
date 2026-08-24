"""Node-identity comparison between the app's manifest and the DAG AE published.

Verifying the installed *version* says "the right image is on the tenant". This
module answers the stronger question: **is the graph that ran the graph we
built?**

The two are not the same check. At AE submit, Heracles re-fetches the manifest
from the tenant-deployed pod and calls ``CreateVersion`` + ``PublishVersion`` on
the same slug the harness seeded, superseding the harness's own seed version. So
what executes is the pod's DAG, not the one the harness uploaded — and nothing
downstream ever names the difference.

Why *identity* and not the DAG blob: template variables are substituted on the
way through (Heracles' ``substituteTemplateVars``, and the harness's own
``{deployment_name}`` / mustache fills before that), so a byte comparison of the
two DAGs fails on every run. What survives substitution is each node's identity
— its name, the app that runs it, and the workflow type dispatched — which is
exactly what "the same graph" turns on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

APP_NAME_PLACEHOLDER = "{app_name}"
"""Manifest placeholder for the connector's own app name.

Both sides of the comparison resolve it against the connector short name, so a
manifest that still ships the placeholder and a copy whose placeholder was
already substituted compare equal rather than reading as a changed node.
"""


@dataclass(frozen=True)
class DagNodeIdentity:
    """What a DAG node is, stripped of everything substitution can rewrite.

    Attributes:
        name: The node's key in the DAG object (``extract``, ``publish``, ...).
        app_name: App the node is dispatched to. Empty when the node declares
            none — absence is not treated as a mismatch, see
            :func:`compare_node_identities`.
        workflow_type: Temporal workflow type the node runs
            (``inputs.workflow_type``). Empty when the node declares none.
    """

    name: str
    app_name: str = ""
    workflow_type: str = ""

    def describe(self) -> str:
        """``extract (app=mysql, workflow_type=MySQLWorkflow)`` — fields it has."""
        parts = [f"app={self.app_name}" if self.app_name else "app=?"]
        parts.append(
            f"workflow_type={self.workflow_type}"
            if self.workflow_type
            else "workflow_type=?"
        )
        return f"{self.name} ({', '.join(parts)})"


@dataclass(frozen=True)
class NodeIdentityChange:
    """One node present on both sides whose identity fields disagree."""

    name: str
    expected: DagNodeIdentity
    actual: DagNodeIdentity


@dataclass(frozen=True)
class ManifestIdentityDiff:
    """Node-set + per-node-identity difference between two DAGs.

    Attributes:
        missing: Nodes the local manifest declares that the published DAG has
            no node for.
        unexpected: Nodes the published DAG carries that the local manifest
            does not declare.
        changed: Nodes on both sides whose ``app_name`` or ``workflow_type``
            conflict.
    """

    missing: tuple[DagNodeIdentity, ...] = ()
    unexpected: tuple[DagNodeIdentity, ...] = ()
    changed: tuple[NodeIdentityChange, ...] = ()

    @property
    def matches(self) -> bool:
        """True iff the two DAGs describe the same nodes doing the same work."""
        return not (self.missing or self.unexpected or self.changed)

    def render(self) -> str:
        """Multi-line diff, one finding per line, ordered most-structural first.

        Node-set differences come before field changes because they are the
        stronger signal: a missing node means the deployed app does not run a
        step this test asserts on, whereas a changed ``workflow_type`` means it
        runs a different build of one.
        """
        if self.matches:
            return "no difference"
        lines: list[str] = []
        for node in self.missing:
            lines.append(
                f"  - declared locally but absent from the published DAG: "
                f"{node.describe()}"
            )
        for node in self.unexpected:
            lines.append(
                f"  + present in the published DAG but not declared locally: "
                f"{node.describe()}"
            )
        for change in self.changed:
            lines.append(
                f"  ~ {change.name}: local {change.expected.describe()} "
                f"vs published {change.actual.describe()}"
            )
        return "\n".join(lines)


def _resolved(raw: object, app_name: str) -> str:
    """A manifest string field as a comparable value.

    Non-strings (a node that carries a number or ``null`` where a name belongs)
    resolve to ``""`` — treated as "not declared", never as a mismatch, because
    the shape is the manifest's problem, not this check's finding.
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if app_name:
        value = value.replace(APP_NAME_PLACEHOLDER, app_name)
    return value


def node_identities(
    dag: Mapping[str, Any], *, app_name: str = ""
) -> dict[str, DagNodeIdentity]:
    """Reduce a DAG object to ``{node name: identity}``.

    ``app_name`` is read from the node level first and from ``inputs`` second:
    the manifest declares both and they agree, but only one may survive a given
    producer's rewriting, so either alone is enough to identify the node.

    Args:
        dag: A manifest-shaped ``dag`` object — ``{node name: node}``.
        app_name: Connector short name used to resolve
            :data:`APP_NAME_PLACEHOLDER`. Pass it on both sides of a comparison
            or on neither; passing it on one side only makes a placeholder read
            as a change.

    Returns:
        One :class:`DagNodeIdentity` per node whose key is a string. Nodes that
        are not objects still yield an identity (name only), so a malformed
        node cannot silently disappear from the node set.
    """
    identities: dict[str, DagNodeIdentity] = {}
    for name, node in dag.items():
        if not isinstance(name, str):
            continue
        if not isinstance(node, dict):
            identities[name] = DagNodeIdentity(name=name)
            continue
        inputs = node.get("inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        resolved_app = _resolved(node.get("app_name"), app_name) or _resolved(
            inputs.get("app_name"), app_name
        )
        identities[name] = DagNodeIdentity(
            name=name,
            app_name=resolved_app,
            workflow_type=_resolved(inputs.get("workflow_type"), app_name),
        )
    return identities


def _conflicts(expected: str, actual: str) -> bool:
    """True only when both sides declared a value and the values differ.

    A field one side omits is not a finding. The published DAG's shape is
    Heracles' to change, and reporting an omission as a mismatch would red every
    leg on the day it stops echoing a field — a false failure about the harness,
    dressed up as a claim about the app under test. The node *set* stays strict;
    this tolerance applies only to the fields inside a node both sides agree
    exists.
    """
    return bool(expected) and bool(actual) and expected != actual


def compare_node_identities(
    expected: Mapping[str, DagNodeIdentity],
    actual: Mapping[str, DagNodeIdentity],
) -> ManifestIdentityDiff:
    """Diff two identity maps built by :func:`node_identities`.

    Args:
        expected: Identities from the local manifest (the app under test).
        actual: Identities from the DAG AE published at submit.

    Returns:
        A :class:`ManifestIdentityDiff`; check :attr:`~ManifestIdentityDiff.matches`.
    """
    missing = tuple(expected[name] for name in sorted(set(expected) - set(actual)))
    unexpected = tuple(actual[name] for name in sorted(set(actual) - set(expected)))
    changed = tuple(
        NodeIdentityChange(name=name, expected=expected[name], actual=actual[name])
        for name in sorted(set(expected) & set(actual))
        if _conflicts(expected[name].app_name, actual[name].app_name)
        or _conflicts(expected[name].workflow_type, actual[name].workflow_type)
    )
    return ManifestIdentityDiff(missing=missing, unexpected=unexpected, changed=changed)
