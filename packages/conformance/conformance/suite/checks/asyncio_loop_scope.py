"""T019 AsyncioTestLoopScopeUnset — flag a broadened pytest-asyncio *fixture*
loop scope with no matching *test* loop scope.

``pytest-asyncio`` has two independent knobs in
``[tool.pytest.ini_options]``:

* ``asyncio_default_fixture_loop_scope`` — the event loop async **fixtures**
  default to.
* ``asyncio_default_test_loop_scope`` — the event loop async **tests** default
  to. **When unset it defaults to ``"function"``** (a fresh loop per test).

Setting the fixture scope to ``session`` / ``package`` / ``module`` / ``class``
without also setting the test scope leaves the two on *different* loops: async
fixtures share one long-lived loop, but each test runs on its own
function-scoped loop. A session-scoped fixture that owns a live resource bound
to *its* loop — a Temporal worker/client, an async DB engine/pool, an
``httpx.AsyncClient``, a message-broker consumer — is then invisible to a test
that drives that resource **from its own body**: the test awaits work the
fixture's loop must service, but that loop is not being driven while the test's
loop runs, so nothing progresses and the test hangs until the suite's timeout
fires.

The failure is silent by construction: tests that only *read* values a fixture
already computed (the common case) pass, so the mismatch hides until someone
writes the first test that awaits fixture-owned work in-body. That is exactly
how it surfaced on a canonical connector — a single ``REUSE`` integration test
that submitted a Temporal workflow from the test body hung for the full
``pytest-timeout`` while every sibling test (which read a class-fixture result)
passed.

Remediation is a one-line config edit: set ``asyncio_default_test_loop_scope``
explicitly — usually to the same scope as the fixtures — so tests and their
fixtures share a loop. Restructuring the offending test to run its async work
inside a same-scope fixture also removes the *hang*, but leaves the config trap
in place for the next author; the config fix is the durable one.

Inline suppression
------------------
Add ``# conformance: ignore[T019] <reason>`` on the
``asyncio_default_fixture_loop_scope`` line (or the line directly above it) when
the mismatch is deliberate — e.g. every async fixture is loop-agnostic and no
test drives fixture-owned work in-body — and state that reason.

Known coverage limits (intentional — biased toward zero false positives at WARN
tier):

* **pyproject-only.** Reads ``[tool.pytest.ini_options]`` from the repo-root
  ``pyproject.toml`` (the fleet convention). A ``pytest.ini`` / ``setup.cfg`` /
  ``tox.ini`` ``[pytest]`` section carrying the same keys is not scanned.
* **Config-level, not usage-level.** The rule fires on the risky *configuration*
  (broad fixture scope, implicit test scope); it does not attempt to prove a
  test actually drives fixture-owned work in-body. A repo that keeps all async
  infra access inside fixtures is currently safe but still warned — suppress
  with a justification, or set the test scope explicitly.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T019 = "T019"

# The two pytest-asyncio ini keys this rule reasons about.
_FIXTURE_SCOPE_KEY = "asyncio_default_fixture_loop_scope"
_TEST_SCOPE_KEY = "asyncio_default_test_loop_scope"

# Valid pytest-asyncio loop scopes (see pytest_asyncio.plugin._ScopeName). Any
# value other than "function" is "broadened" — fixtures then outlive a single
# test's loop, which is only safe if tests share that loop too.
_VALID_SCOPES = frozenset({"session", "package", "module", "class", "function"})

# ``asyncio_default_fixture_loop_scope = ...`` assignment line (for anchoring the
# finding + its suppression directive).
_FIXTURE_SCOPE_LINE_RE = re.compile(rf"^\s*{re.escape(_FIXTURE_SCOPE_KEY)}\s*=")

__all__ = [
    "SERIES",
    "discover",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


# ---------------------------------------------------------------------------
# Pure core (filesystem-free — unit-testable)
# ---------------------------------------------------------------------------


def _ini_options(text: str) -> dict[str, object] | None:
    """Return ``[tool.pytest.ini_options]`` from *text*, or ``None``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tool = data.get("tool")
    pytest_tbl = tool.get("pytest") if isinstance(tool, dict) else None
    ini = pytest_tbl.get("ini_options") if isinstance(pytest_tbl, dict) else None
    return ini if isinstance(ini, dict) else None


def _fixture_scope_line(text: str) -> int:
    for i, line in enumerate(text.splitlines(), start=1):
        if _FIXTURE_SCOPE_LINE_RE.match(line):
            return i
    return 1


def _message(fixture_scope: str) -> str:
    return (
        f"pyproject sets {_FIXTURE_SCOPE_KEY}='{fixture_scope}' but leaves "
        f"{_TEST_SCOPE_KEY} unset (it defaults to 'function'): async fixtures "
        f"share one '{fixture_scope}'-scoped event loop while each test runs on "
        "its own function-scoped loop. A test that drives a fixture-owned "
        "resource bound to the fixture loop — a Temporal worker/client, an async "
        "DB engine/pool, an httpx.AsyncClient — from its own body then awaits "
        "work that loop never runs, so it hangs until the suite timeout fires. "
        f"Set {_TEST_SCOPE_KEY} explicitly (usually '{fixture_scope}', to match "
        "the fixtures) so tests and their fixtures share a loop. See T019."
    )


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a ``pyproject.toml`` *text* for T019.

    Fires when ``asyncio_default_fixture_loop_scope`` is set to a valid
    non-``function`` scope while ``asyncio_default_test_loop_scope`` is absent.
    Self-contained — the risk is entirely in the config, so no filesystem/test
    context is needed (contrast T018).
    """
    ini = _ini_options(text)
    if ini is None:
        return []
    fixture_scope = ini.get(_FIXTURE_SCOPE_KEY)
    # Only a *broadened*, valid fixture scope is a risk; "function" (or an
    # invalid value pytest-asyncio would itself reject) is not.
    if not isinstance(fixture_scope, str) or fixture_scope not in _VALID_SCOPES:
        return []
    if fixture_scope == "function":
        return []
    # An explicit test scope — any value — means the author made a deliberate
    # choice; the trap is only the *implicit* function default.
    if _TEST_SCOPE_KEY in ini:
        return []
    return [
        make_toml_finding(
            rule_id=RULE_T019,
            file=file,
            line=_fixture_scope_line(text),
            column=1,
            message=_message(fixture_scope),
            suppressions=parse_toml_suppressions(text),
        )
    ]


# ---------------------------------------------------------------------------
# Filesystem scan API
# ---------------------------------------------------------------------------


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan the repo-root ``pyproject.toml`` for T019."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Cross-file entry point (used by the standalone CLI)."""
    findings: list[Finding] = []
    for path in paths:
        if path.name == "pyproject.toml":
            findings.extend(scan_path(path, root))
    return findings


def discover(root: Path) -> list[Path]:
    """Discover the repo-root ``pyproject.toml`` (``[]`` when absent → no-op)."""
    pyproject = root / "pyproject.toml"
    return [pyproject] if pyproject.is_file() else []


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "T019: flag a broadened pytest-asyncio fixture loop scope "
        "(asyncio_default_fixture_loop_scope) with no matching "
        "asyncio_default_test_loop_scope, which can hang tests that drive "
        "fixture-owned async resources from the test body."
    ),
    discover=discover,
    default_scan_paths=("pyproject.toml",),
)
"""CLI entry point for the T019 asyncio-loop-scope check."""


if __name__ == "__main__":
    sys.exit(main())
