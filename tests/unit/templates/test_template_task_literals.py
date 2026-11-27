"""No hand-picked numbers in a template ``@task(...)`` (FND-5224, ADR-0018).

A numeric literal on a template's ``@task`` beats the process-wide defaults
(``ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS``, ``ATLAN_HEARTBEAT_TIMEOUT_SECONDS``)
that ``docs/concepts/tasks.md`` tells operators to tune. In a template that
means every app built on it silently ignores the documented knob — the only
escape is overriding each method. ``SqlApp`` shipped exactly that on all twelve
``extract_*`` / ``transform_*`` tasks (1800 / 120 / 30 s) until this guard.

The guard is an AST scan, not a metadata check, so it also catches a literal on
a task that happens to equal today's default. A literal is allowed only through
``ALLOWED`` with a reason.

Deprecated template modules are out of scope: they are frozen until removal and
their apps are migrating to ``SqlApp``. ``DEPRECATED_MODULES`` lists them, and a
test below fails if one of them disappears so the list cannot rot.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import application_sdk.templates as templates_pkg

TEMPLATES_DIR = Path(templates_pkg.__file__).parent

#: Modules whose templates are deprecated (removal in v4.0.0). Not scanned.
DEPRECATED_MODULES = frozenset(
    {
        "sql_metadata_extractor.py",
        "incremental_sql_metadata_extractor.py",
        "sql_query_extractor.py",
        # Only task is the deprecated ``upload_to_atlan`` redirect.
        "base_metadata_extractor.py",
    }
)

#: ``(module, task, keyword) -> reason``. Each entry is a deliberate exception.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("_sql_app_prime.py", "prime_sql_auth", "timeout_seconds"): (
        "fixed work — one SELECT 1 plus credential resolution, independent of "
        "source size — and retry_max_attempts=1 makes a hung probe terminal, so "
        "the 24h backstop would hold the workflow a day on a dead endpoint"
    ),
    ("_sql_app_prime.py", "prime_sql_auth", "retry_max_attempts"): (
        "load-bearing: an activity retry re-runs the auth handshake and "
        "re-stacks failed_login_attempts on the source (BLDX-1295)"
    ),
}


def _is_task_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "task") or (
        isinstance(func, ast.Attribute) and func.attr == "task"
    )


def _contains_numeric_literal(node: ast.expr) -> bool:
    """True if any number appears anywhere in the keyword's expression.

    Walks the whole subtree, so ``60 * 60``, ``-1`` and ``BUDGET * 2`` all
    count: each is still a hand-picked number in the decorator.
    """
    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, (int, float))
        and not isinstance(sub.value, bool)
        for sub in ast.walk(node)
    )


def _numeric_task_keywords(source: str) -> list[tuple[str, str, int]]:
    """Return ``(function, keyword, lineno)`` for every numeric ``@task`` kwarg."""
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not _is_task_call(deco):
                continue
            assert isinstance(deco, ast.Call)
            for kw in deco.keywords:
                if kw.arg is not None and _contains_numeric_literal(kw.value):
                    found.append((node.name, kw.arg, kw.value.lineno))
    return found


def _scanned_modules() -> list[Path]:
    return sorted(
        p
        for p in TEMPLATES_DIR.rglob("*.py")
        if p.relative_to(TEMPLATES_DIR).as_posix() not in DEPRECATED_MODULES
    )


def test_no_numeric_literal_in_template_task_keywords() -> None:
    offenders: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for path in _scanned_modules():
        rel = path.relative_to(TEMPLATES_DIR).as_posix()
        for func, kw, lineno in _numeric_task_keywords(path.read_text()):
            key = (rel, func, kw)
            if key in ALLOWED:
                seen.add(key)
                continue
            offenders.append(f"templates/{rel}:{lineno} {func}: {kw}=<literal>")
    assert not offenders, (
        "template @task declares a hand-picked number, which overrides the "
        "env-tunable defaults for every app on the template. Drop it, or add "
        "an ALLOWED entry with the reason the work is fixed-size:\n  "
        + "\n  ".join(offenders)
    )
    stale = set(ALLOWED) - seen
    assert not stale, f"ALLOWED entries no longer match any literal: {stale}"


@pytest.mark.parametrize("name", sorted(DEPRECATED_MODULES))
def test_deprecated_exclusions_still_exist(name: str) -> None:
    """Drop a module from DEPRECATED_MODULES when it is deleted."""
    assert (TEMPLATES_DIR / name).is_file()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "@task(timeout_seconds=1800)\nasync def f(self): ...",
            [("f", "timeout_seconds")],
        ),
        (
            "@app.task(heartbeat_timeout_seconds=1.5)\ndef f(): ...",
            [("f", "heartbeat_timeout_seconds")],
        ),
        (
            "@task(auto_heartbeat_seconds=-1)\ndef f(): ...",
            [("f", "auto_heartbeat_seconds")],
        ),
        (
            "@task(timeout_seconds=60 * 60)\ndef f(): ...",
            [("f", "timeout_seconds")],
        ),
        (
            "@task(timeout_seconds=BUDGET * 2)\ndef f(): ...",
            [("f", "timeout_seconds")],
        ),
        (
            "@task(timeout_seconds=int(1800.0))\ndef f(): ...",
            [("f", "timeout_seconds")],
        ),
        ("@task()\ndef f(): ...", []),
        ("@task(timeout_seconds=BUDGET)\ndef f(): ...", []),
        ("@task(enabled=True)\ndef f(): ...", []),
        ("@other(timeout_seconds=5)\ndef f(): ...", []),
    ],
)
def test_detector(source: str, expected: list[tuple[str, str]]) -> None:
    """The scanner sees what it claims to — a blind guard passes vacuously."""
    assert [(f, k) for f, k, _ in _numeric_task_keywords(source)] == expected
