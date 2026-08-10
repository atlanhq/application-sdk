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


# ── The token has to reach a value to be dangerous ───────────────────────────
#
# The exclusions below exist because without them the rule fires on the very
# module that implements the correct behaviour (application_sdk.common.task_queue
# defines the token it substitutes, documents its fields as attribute
# docstrings, and the handler logs at WARNING/ERROR naming the token it could
# not resolve). Each of these was a real finding against that code.


def test_o005_silent_on_attribute_docstring() -> None:
    """A PEP 257 attribute docstring is not ``body[0]`` of its class, so the
    original first-statement-only exclusion missed it."""
    src = 'class C:\n    x: int\n    """Holds the {app_name} token."""\n'
    assert "O005" not in _o_ids(src)


def test_o005_silent_on_logging_call() -> None:
    """Code reporting an unresolved token has to quote it; a log message is
    never dispatched as an identifier."""
    src = 'logger.error("unresolved {app_name} token in manifest %s", source)\n'
    assert "O005" not in _o_ids(src)


def test_o005_silent_on_raise_message() -> None:
    src = 'raise ValueError("unresolved {app_name} token")\n'
    assert "O005" not in _o_ids(src)


def test_o005_silent_on_token_sentinel_definition() -> None:
    """Declaring the token itself is not freezing it into an identifier —
    ``application_sdk.common.task_queue.APP_NAME_TOKEN`` is exactly this."""
    assert "O005" not in _o_ids('APP_NAME_TOKEN = "{app_name}"\n')


def test_o005_silent_on_prose_named_constant() -> None:
    assert "O005" not in _o_ids(
        '_MESSAGE = "the {app_name} token was left unresolved"\n'
    )


# ── ...but the exclusions must stay narrow ───────────────────────────────────


def test_o005_fires_on_all_caps_queue_template() -> None:
    """An ALL_CAPS name is not a free pass: this one is a genuine queue template,
    neither bare-token nor prose-named, so the sentinel exclusion must not
    swallow it."""
    assert "O005" in _o_ids('TASK_QUEUE = "atlan-{app_name}-prod"\n')


def test_o005_fires_on_bare_token_bound_to_identifier() -> None:
    """Only an ALL_CAPS binding reads as a token declaration; a lowercase one is
    a value that will be dispatched."""
    assert "O005" in _o_ids('task_queue = "{app_name}"\n')


def test_o005_fires_on_keyword_argument() -> None:
    assert "O005" in _o_ids('submit(task_queue="atlan-{app_name}-prod")\n')


def test_o005_fires_at_any_depth_in_a_dag_literal() -> None:
    """AE DAG nodes nest the queue several levels down."""
    src = 'dag = {"extract": {"inputs": {"task_queue": "atlan-{app_name}-prod"}}}\n'
    assert "O005" in _o_ids(src)


def test_o005_fires_on_returned_template() -> None:
    src = 'def build():\n    return "atlan-{app_name}-prod"\n'
    assert "O005" in _o_ids(src)
