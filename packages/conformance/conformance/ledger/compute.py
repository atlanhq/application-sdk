"""Compute IRR (Integration Readiness Ratio) from the Integration Lane Ledger.

    IRR = workflows with a lane at realism in {L,R} AND depth in {G,V}
          AND a *verified* automatic, green cadence
          -----------------------------------------------------------
          total product workflows

Two of the three axes are re-derived rather than trusted:

* the **denominator** is AST-scanned from the repo's ``@entrypoint``
  declarations, so adding a workflow fails the ledger until someone classifies
  it;
* **cadence** comes from the GitHub Actions API, so writing ``cadence: A`` in
  the ledger earns nothing if the job does not actually run on an automatic
  trigger;
* the **boundary** claim is checked against the cited test, so a lane cannot be
  labelled ``transformed`` while demonstrably verifying against a live tenant.

Only ``realism`` and ``depth`` are trusted from the ledger. They are
citation-backed, reviewed like code, and audited by the quarterly mutation
sample.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from conformance.ledger.schema import Boundary, ConnectorLedger, Ledger, Workflow

#: Workflow triggers that count as automatic.
AUTOMATIC_TRIGGERS = frozenset({"push", "pull_request", "schedule"})

#: Decorator names that declare a product workflow.
ENTRYPOINT_DECORATORS = frozenset({"entrypoint"})

#: Markers that a lane reads back from a live Atlan tenant, i.e. it verified
#: something *after* the handoff and is therefore an e2e lane.
#:
#: Deliberately not a list of system-app names. Which system app a DAG invokes
#: varies per connector (publish, lineage, QI, or several); needing tenant
#: credentials to verify the result does not. All four full-DAG suites in the
#: fleet gate on exactly these two environment variables via the same SDK base
#: class, so one uniform rule covers every repo.
TENANT_ACCESS_MARKERS = (
    "ATLAN_BASE_URL",
    "ATLAN_API_KEY",
    "application_sdk.testing.e2e",
    "pyatlan",
)

#: Base classes whose ``run()`` override is itself the product workflow.
#: Three connectors (monte-carlo, mssql, fivetran) ship a single workflow this
#: way instead of decorating an ``@entrypoint``; without this they would get no
#: denominator verification at all.
APP_BASE_CLASSES = frozenset(
    {"App", "BaseMetadataExtractor", "SqlApp", "BaseSQLMetadataExtractor"}
)


class WorkflowRun(Protocol):
    """The slice of a GitHub Actions run this module needs."""

    @property
    def trigger(self) -> str: ...

    @property
    def conclusion(self) -> str: ...

    @property
    def age_days(self) -> int: ...


class ActionsClient(Protocol):
    """Injected so the scorer is testable without network access."""

    def latest_run(
        self, repo: str, workflow_file: str, job: str | None = None
    ) -> WorkflowRun | None: ...


class BoundaryMismatchError(RuntimeError):
    """A lane claims to stop at the handoff but reads back from a tenant.

    Only raised in the score-inflating direction. Claiming ``post-publish``
    when no tenant access is detected is merely conservative and is accepted
    silently — detection is a lower bound, not a proof of absence.
    """

    def __init__(self, connector: str, workflow: str, path: str, marker: str) -> None:
        super().__init__(
            f"{connector}/{workflow}: lane is classified "
            f'boundary="transformed" but {path} references {marker!r}, which '
            f"means it verifies against a live Atlan tenant — i.e. it runs past "
            f"the handoff artifact and is an e2e lane. Reclassify it as "
            f'boundary="post-publish", or cite the integration lane instead.'
        )
        self.connector = connector
        self.workflow = workflow
        self.path = path
        self.marker = marker


class LedgerDriftError(RuntimeError):
    """The ledger no longer matches the code it claims to describe."""

    def __init__(self, connector: str, missing: set[str], stale: set[str]) -> None:
        parts = [f"ledger drift in {connector}:"]
        if missing:
            parts.append(
                f"  {len(missing)} entrypoint(s) declared in code but absent from "
                f"the ledger: {', '.join(sorted(missing))}"
            )
        if stale:
            parts.append(
                f"  {len(stale)} ledger row(s) with no matching entrypoint in code: "
                f"{', '.join(sorted(stale))}"
            )
        parts.append("  Classify or exclude each, then re-run.")
        super().__init__("\n".join(parts))
        self.connector = connector
        self.missing = missing
        self.stale = stale


def scan_entrypoints(repo_path: Path) -> set[str]:
    """Return every product-workflow declaration in an app repo.

    Two shapes count, matching what the fleet actually ships:

    * an ``@entrypoint``-decorated method (7 of the top-10 connectors);
    * a ``run()`` override on an ``App``/``BaseMetadataExtractor``/``SqlApp``
      subclass, which is how monte-carlo, mssql and fivetran declare their
      single workflow.

    Skips vendored and virtual-env trees. A syntactically broken module is
    skipped rather than failing the whole scan — the conformance suite has its
    own rules for that.
    """
    entrypoints: set[str] = set()
    run_overrides: set[str] = set()
    app_dir = repo_path / "app"
    if not app_dir.is_dir():
        return entrypoints

    for py in app_dir.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _has_entrypoint_decorator(node):
                    entrypoints.add(node.name)
            elif isinstance(node, ast.ClassDef) and _is_app_subclass(node):
                run_overrides.update(
                    child.name
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == "run"
                )

    # ``run()`` is a product workflow only in the single-workflow pattern. When
    # the app also declares ``@entrypoint`` methods (databricks, powerbi), its
    # ``run()`` is the SDK orchestrator those entrypoints dispatch through, not
    # a workflow a user can trigger.
    return entrypoints or run_overrides


def _is_app_subclass(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = (
            base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
        )
        if name in APP_BASE_CLASSES:
            return True
    return False


def _has_entrypoint_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", None)
        )
        if name in ENTRYPOINT_DECORATORS:
            return True
    return False


def detect_tenant_access(repo_path: Path, test_ref: str) -> tuple[str, str] | None:
    """Return ``(path, marker)`` if the cited lane reads back from a tenant.

    ``test_ref`` may be a file or a directory; directories are scanned one level
    deep for ``test_*.py``. Returns ``None`` when nothing is found — which is a
    lower bound, not proof the lane stays inside the boundary.
    """
    target = repo_path / test_ref
    if target.is_file():
        candidates = [target]
    elif target.is_dir():
        candidates = sorted(target.rglob("test_*.py"))
    else:
        return None

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in TENANT_ACCESS_MARKERS:
            if marker in text:
                return (str(path.relative_to(repo_path)), marker)
    return None


@dataclass
class WorkflowResult:
    """Per-workflow verdict, with the reason it did or did not qualify."""

    id: str
    covered: bool
    reason: str


@dataclass
class ConnectorResult:
    """Per-connector IRR plus the per-workflow detail behind it."""

    name: str
    workflows: list[WorkflowResult] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return sum(1 for w in self.workflows if w.covered)

    @property
    def total(self) -> int:
        return len(self.workflows)

    @property
    def irr(self) -> float:
        return (self.covered / self.total) if self.total else 0.0


@dataclass
class FleetResult:
    """Fleet roll-up. IRR is workflow-weighted, not a mean of per-app ratios."""

    connectors: list[ConnectorResult] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return sum(c.covered for c in self.connectors)

    @property
    def total(self) -> int:
        return sum(c.total for c in self.connectors)

    @property
    def irr(self) -> float:
        return (self.covered / self.total) if self.total else 0.0


def evaluate_connector(
    entry: ConnectorLedger,
    repo_path: Path | None,
    actions: ActionsClient | None,
) -> ConnectorResult:
    """Score one connector, verifying denominator and cadence where possible."""
    if repo_path is not None:
        declared = scan_entrypoints(repo_path)
        if declared:
            accounted = entry.declared_ids
            missing = declared - accounted
            stale = {w.id for w in entry.workflows} - declared
            if missing or stale:
                raise LedgerDriftError(entry.name, missing, stale)

    result = ConnectorResult(name=entry.name)
    for wf in entry.workflows:
        if repo_path is not None and wf.lane.boundary is Boundary.TRANSFORMED:
            _assert_boundary(entry.name, wf, repo_path)
        result.workflows.append(_evaluate_workflow(entry.name, wf, actions))
    return result


def _assert_boundary(connector: str, wf: Workflow, repo_path: Path) -> None:
    """Fail when a lane claims to stop at the handoff but demonstrably does not."""
    test_ref = wf.lane.evidence.test
    if not test_ref:
        return
    hit = detect_tenant_access(repo_path, test_ref)
    if hit is not None:
        raise BoundaryMismatchError(connector, wf.id, hit[0], hit[1])


def _evaluate_workflow(
    connector: str, wf: Workflow, actions: ActionsClient | None
) -> WorkflowResult:
    lane = wf.lane

    if lane.boundary is not Boundary.TRANSFORMED:
        return WorkflowResult(
            wf.id,
            False,
            f"lane is boundary={lane.boundary.value}; it verifies past the "
            f"handoff artifact, so it is an e2e lane",
        )

    if not lane.qualifies_on_declared_axes:
        return WorkflowResult(
            wf.id,
            False,
            f"lane is realism={lane.realism.value} depth={lane.depth.value}; "
            f"needs realism in (L,R) and depth in (G,V)",
        )

    workflow_file = lane.evidence.ci_workflow
    if not workflow_file:
        return WorkflowResult(wf.id, False, "lane is not wired into any CI workflow")

    if actions is None:
        # No client injected (offline/dry-run): fall back to the declared
        # cadence and say so, rather than silently crediting.
        declared_auto = lane.cadence.value == "A"
        return WorkflowResult(
            wf.id,
            declared_auto,
            "declared cadence used; not verified against GitHub Actions",
        )

    run = actions.latest_run(connector, workflow_file, lane.evidence.ci_job)
    if run is None:
        return WorkflowResult(wf.id, False, f"no runs found for {workflow_file}")
    if run.trigger not in AUTOMATIC_TRIGGERS:
        return WorkflowResult(
            wf.id,
            False,
            f"latest run triggered by {run.trigger!r}, not an automatic trigger "
            f"({lane.evidence.gate or 'gated'})",
        )
    if run.conclusion != "success":
        return WorkflowResult(
            wf.id, False, f"latest automatic run concluded {run.conclusion!r}"
        )

    return WorkflowResult(wf.id, True, f"verified green on {run.trigger}")


def evaluate(
    ledger: Ledger,
    repo_root: Path | None = None,
    actions: ActionsClient | None = None,
    only: Iterable[str] | None = None,
) -> FleetResult:
    """Score the fleet.

    ``repo_root`` is the directory holding the connector checkouts; when it is
    ``None`` the denominator is taken from the ledger without verification.
    """
    wanted = set(only) if only else None
    fleet = FleetResult()
    for name, entry in ledger.connectors.items():
        if wanted and name not in wanted:
            continue
        repo_path = (repo_root / name) if repo_root else None
        if repo_path is not None and not repo_path.is_dir():
            repo_path = None
        fleet.connectors.append(evaluate_connector(entry, repo_path, actions))
    return fleet
