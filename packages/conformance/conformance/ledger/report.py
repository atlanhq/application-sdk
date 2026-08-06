"""Score one connector's integration coverage from derived facts.

    IRR = workflows with a declared integration lane that validates the app's
          own transformed output, on an automatic green CI job
          ────────────────────────────────────────────────────────────────────
          the app's product workflows

Every input is derived (see :mod:`.derive`) or observed (the GitHub Actions
API). Nothing is stored, so nothing goes stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from conformance.ledger.compute import ActionsClient, WorkflowRun
from conformance.ledger.derive import (
    _dag_nodes_to_workflow,
    discover_declared_coverage,
    discover_workflows,
)
from conformance.ledger.schema import QUALIFYING_DEPTH, Depth

#: Triggers that count as automatic.
AUTOMATIC_TRIGGERS = frozenset({"push", "pull_request", "schedule"})


@dataclass
class WorkflowVerdict:
    """Why one product workflow does or does not count."""

    id: str
    declared_at: str
    covered: bool
    depth: Depth
    reason: str


@dataclass
class ConnectorReport:
    name: str
    workflows: list[WorkflowVerdict] = field(default_factory=list)
    orphan_declarations: list[str] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return sum(1 for w in self.workflows if w.covered)

    @property
    def total(self) -> int:
        return len(self.workflows)

    @property
    def irr(self) -> float:
        return (self.covered / self.total) if self.total else 0.0


def _cadence_ok(
    connector: str,
    ci_workflow: str | None,
    actions: ActionsClient | None,
) -> tuple[bool, str]:
    if actions is None:
        return True, "cadence not verified (no GitHub client)"
    if not ci_workflow:
        return False, "no CI workflow known for this lane"

    run: WorkflowRun | None = actions.latest_run(connector, ci_workflow, None)
    if run is None:
        return False, f"no runs found for {ci_workflow}"
    if run.trigger not in AUTOMATIC_TRIGGERS:
        return False, f"latest run was {run.trigger!r}, not an automatic trigger"
    if run.conclusion != "success":
        return False, f"latest automatic run concluded {run.conclusion!r}"
    return True, f"verified green on {run.trigger}"


def evaluate_repo(
    repo: Path,
    connector: str,
    actions: ActionsClient | None = None,
    ci_workflow: str | None = None,
) -> ConnectorReport:
    """Score one connector checkout."""
    report = ConnectorReport(name=connector)

    workflows = discover_workflows(repo)
    coverage = discover_declared_coverage(repo)

    # Workflows the DAG says are the app's own work. A workflow with no DAG node
    # is still counted - it is shipped code either way - but its absence from
    # the manifest is worth surfacing.
    dag_workflows = {w for w in _dag_nodes_to_workflow(repo, connector).values() if w}

    for name, declared_at in sorted(workflows.items()):
        depth = coverage.depth.get(name, Depth.NONE)

        if depth is Depth.NONE:
            reason = (
                "no integration lane declares it (add entrypoint=... to a "
                "workflow Scenario - see T020)"
            )
            report.workflows.append(
                WorkflowVerdict(name, declared_at, False, depth, reason)
            )
            continue

        if depth not in QUALIFYING_DEPTH:
            reason = (
                f"covered at depth {depth.value} - the lane starts the workflow "
                f"but declares no output validation (set schema_base_path or "
                f"expected_data)"
            )
            report.workflows.append(
                WorkflowVerdict(name, declared_at, False, depth, reason)
            )
            continue

        ok, reason = _cadence_ok(connector, ci_workflow, actions)
        report.workflows.append(WorkflowVerdict(name, declared_at, ok, depth, reason))

    # A declaration naming a workflow the app does not have is a real defect:
    # the suite is starting something that no longer exists, or was renamed.
    for declared in sorted(coverage.depth):
        if declared not in workflows:
            report.orphan_declarations.append(declared)

    if dag_workflows:
        for verdict in report.workflows:
            if verdict.id not in dag_workflows and verdict.covered:
                verdict.reason += " (not present in any generated manifest DAG)"

    return report
