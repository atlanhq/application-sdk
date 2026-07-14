"""T017 — e2e harness agent_spec() must inherit the per-leg deployment queue.

The companion to T016 (:mod:`conformance.suite.checks.e2e_deployment_name`).
T016 polices the *worker* side (the compose overlay must inherit the sdr-e2e
per-leg ``ATLAN_DEPLOYMENT_NAME``); T017 polices the *harness* side.

The full-DAG worker derives its Temporal queue as
``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``, and the harness
derives the extract-node queue it dispatches to from the SAME two env vars via
``BaseE2ETest.agent_spec`` (``agent_name = {app}-{deployment}`` →
``atlan-{agent_name}``). The sdr-e2e action exports a per-leg
``ATLAN_DEPLOYMENT_NAME`` (``e2e-full-ci-<run_id>[-<matrix-leg>]``) to
``$GITHUB_ENV`` so both sides land on one queue.

A connector's e2e test that *overrides* ``agent_spec`` with a hard-coded
``agent_name`` (e.g. ``AgentSpec(agent_name=f"metabase-e2e-full-ci-{self.run_id}")``)
that neither reads ``ATLAN_DEPLOYMENT_NAME`` nor defers to ``super().agent_spec()``
pins the harness to the un-suffixed queue ``atlan-<app>-e2e-full-ci-<run_id>``.
Once the worker (correctly) inherits the leg-suffixed value, the two queues
diverge, no worker polls the harness's queue, the extract node stays Running,
and the run hangs ("No Workers Running"). This is exactly the atlan-metabase-app
regression: overlay fixed (T016) but agent_spec left hard-coded.

* ``T017`` — E2EAgentSpecPinsQueue: an ``agent_spec`` override under ``tests/``
  returns a hard-coded ``AgentSpec(agent_name=...)`` without referencing
  ``ATLAN_DEPLOYMENT_NAME`` or calling ``super().agent_spec()``.

The compliant shape defers to the base env-derivation when the deployment env
is present, keeping a run-id fallback for local runs (mirrors
``SQLAppE2ETest.agent_spec``)::

    def agent_spec(self) -> AgentSpec:
        if os.environ.get("ATLAN_APPLICATION_NAME") and os.environ.get(
            "ATLAN_DEPLOYMENT_NAME"
        ):
            return super().agent_spec()
        return AgentSpec(agent_name=f"myconn-e2e-full-ci-{self.run_id}")

Discovery
---------
Walks the whole ``tests/`` tree (an ``agent_spec`` override may live in an e2e
test file or a shared harness base under ``tests/``). A connector that does not
override ``agent_spec`` at all (inheriting the SDK's env-derived default, as the
SQL apps do) is never flagged.

Inline suppression
------------------
``# conformance: ignore[T017] <reason>`` on the ``def agent_spec`` line.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    make_cli_main,
    make_finding,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T017 = "T017"

_DEPLOYMENT_ENV = "ATLAN_DEPLOYMENT_NAME"
_AGENT_SPEC = "agent_spec"
_AGENTSPEC_CTOR = "AgentSpec"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Walk ``tests/`` for all Python source files."""
    base = root / "tests"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _is_agentspec_call(node: ast.expr) -> bool:
    """True when *node* is a call to ``AgentSpec(...)`` (bare or attribute)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _AGENTSPEC_CTOR
    if isinstance(func, ast.Attribute):
        return func.attr == _AGENTSPEC_CTOR
    return False


def _returns_hardcoded_agentspec(fn: ast.FunctionDef) -> bool:
    """True when the function returns ``AgentSpec`` with a hard-coded agent_name.

    A literal ``agent_name`` is a ``Constant`` (plain string) or ``JoinedStr``
    (f-string, e.g. ``f"...-{self.run_id}"``) — the hard-coded shape. A value
    built by a call or from the environment is not treated as hard-coded.

    ``agent_name`` is ``AgentSpec``'s first (and only required) field, so both
    the keyword form ``AgentSpec(agent_name="x")`` and the positional form
    ``AgentSpec("x")`` / ``AgentSpec(f"...{self.run_id}")`` are inspected — the
    positional form is valid Python and would otherwise escape detection.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or not _is_agentspec_call(node.value):
            continue
        call = node.value
        assert isinstance(call, ast.Call)
        # Positional: first arg is agent_name.
        if call.args and isinstance(call.args[0], (ast.Constant, ast.JoinedStr)):
            return True
        # Keyword: agent_name=...
        for kw in call.keywords:
            if kw.arg == "agent_name" and isinstance(
                kw.value, (ast.Constant, ast.JoinedStr)
            ):
                return True
    return False


def _defers_to_env(fn: ast.FunctionDef) -> bool:
    """True when the function reads ATLAN_DEPLOYMENT_NAME or calls super().agent_spec().

    Either signal means the override consults the per-leg deployment env (or the
    base env-derivation) rather than pinning a hard-coded queue.
    """
    for node in ast.walk(fn):
        # `ATLAN_DEPLOYMENT_NAME` referenced as a string literal (os.environ.get(...)).
        if isinstance(node, ast.Constant) and node.value == _DEPLOYMENT_ENV:
            return True
        # super().agent_spec() call.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == _AGENT_SPEC
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
        ):
            return True
    return False


def _message(fn_name: str) -> str:
    return (
        f"{_AGENT_SPEC}() override returns a hard-coded AgentSpec(agent_name=...) "
        f"that neither reads {_DEPLOYMENT_ENV} nor calls super().{_AGENT_SPEC}(). "
        "It pins the harness's extract queue to atlan-<app>-e2e-full-ci-<run_id> "
        "(no matrix-leg suffix), so once the worker inherits the sdr-e2e per-leg "
        f"{_DEPLOYMENT_ENV} the two land on different Temporal queues and the run "
        'hangs ("No Workers Running"). Defer to the base env-derivation when '
        f"ATLAN_APPLICATION_NAME + {_DEPLOYMENT_ENV} are set (return "
        f"super().{_AGENT_SPEC}()), keeping the run-id name only as a local "
        "fallback — see SQLAppE2ETest.agent_spec."
    )


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one test-file *text* for T017 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    directives = _parse_directives(text)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != _AGENT_SPEC:
            continue
        if not _returns_hardcoded_agentspec(node):
            continue
        if _defers_to_env(node):
            continue
        findings.append(
            make_finding(
                filename=file,
                rule_id=RULE_T017,
                node=node,
                message=_message(node.name),
                directives=directives,
            )
        )
    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single test file for T017 findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    description=(
        "T017: e2e agent_spec() overrides must inherit the per-leg deployment "
        "queue (read ATLAN_DEPLOYMENT_NAME / call super), not hard-code it."
    ),
    discover=discover,
    default_scan_paths=("tests",),
)
"""CLI entry point for the T017 e2e-agent-spec check."""


if __name__ == "__main__":
    sys.exit(main())
