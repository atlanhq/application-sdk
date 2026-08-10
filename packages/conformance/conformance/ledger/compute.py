"""Shared primitives for integration-coverage scoring.

Scoring itself lives in :mod:`.report`, built on facts derived in
:mod:`.derive`. What remains here is what both need:

``WorkflowRun`` / ``ActionsClient``
    Protocols for the CI-cadence lookup, so scoring stays pure and testable
    offline while the ``gh``-backed implementation lives in the CLI.

``scan_entrypoints``
    The product-workflow scan, kept as a standalone entry point for callers
    that want the denominator without a full report.

``detect_tenant_access``
    A cross-check on the integration/e2e line. The boundary itself is derived
    from the generated AE manifest, but a suite can still overreach: a lane
    that verifies past the handoff needs Atlan tenant credentials to do so, and
    every full-DAG suite in the fleet gates on exactly these markers. Useful
    for flagging a "integration" lane that is really an e2e one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Protocol

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
#: Some connectors ship a single workflow this way instead of decorating an
#: ``@entrypoint``; without this they would get no denominator verification at
#: all.
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


def scan_entrypoints(repo_path: Path) -> set[str]:
    """Return every product-workflow declaration in an app repo.

    Two shapes count, matching what the fleet actually ships:

    * an ``@entrypoint``-decorated method (7 of the top-10 connectors);
    * a ``run()`` override on an ``App``/``BaseMetadataExtractor``/``SqlApp``
      subclass, which is how single-workflow connectors declare their
      one workflow.

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
    # an app also declares ``@entrypoint`` methods, its ``run()`` is the SDK
    # orchestrator those entrypoints dispatch through, not a workflow a user
    # can trigger.
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
