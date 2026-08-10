"""Tests for O005 UnresolvedAppNamePlaceholder (CONNECT-183/CONNECT-191).

Exercised through the optimizations package ``scan_text`` so the real wiring
(directive parsing, suppression handling) is covered, not just the bare
detector.
"""

from __future__ import annotations

from conformance.suite.checks.optimizations import scan_text as o_scan


def _o_ids(src: str) -> list[str]:
    return [f.rule_id for f in o_scan(src, "app/x.py") if not f.suppressed]


def test_o005_fires_on_bare_literal_assignment() -> None:
    assert "O005" in _o_ids('task_queue = "atlan-{app_name}-production"\n')


def test_o005_fires_on_call_argument() -> None:
    assert "O005" in _o_ids('register_queue("atlan-{app_name}-production")\n')


def test_o005_fires_on_dict_value() -> None:
    assert "O005" in _o_ids('inputs = {"task_queue": "atlan-{app_name}-production"}\n')


def test_o005_silent_on_fstring() -> None:
    assert "O005" not in _o_ids('task_queue = f"atlan-{app_name}-production"\n')


def test_o005_silent_on_resolving_format_call() -> None:
    assert "O005" not in _o_ids(
        'task_queue = "atlan-{app_name}-production".format(app_name=app_name)\n'
    )


def test_o005_fires_on_format_call_missing_app_name_kwarg() -> None:
    # .format() is called, but app_name is never bound — still unresolved.
    assert "O005" in _o_ids(
        'task_queue = "atlan-{app_name}-{deployment}".format(deployment=d)\n'
    )


def test_o005_silent_on_module_docstring() -> None:
    assert "O005" not in _o_ids(
        '"""Task queues follow the atlan-{app_name}-<deployment> convention."""\n'
    )


def test_o005_silent_on_function_docstring() -> None:
    src = (
        "def f():\n"
        '    """Builds atlan-{app_name}-<deployment> queue names."""\n'
        "    return None\n"
    )
    assert "O005" not in _o_ids(src)


def test_o005_silent_without_token() -> None:
    assert "O005" not in _o_ids('task_queue = "atlan-connector-production"\n')


def test_o005_suppressed_inline() -> None:
    src = 'task_queue = "atlan-{app_name}-production"  # conformance: ignore[O005] legacy template\n'
    assert "O005" not in _o_ids(src)
