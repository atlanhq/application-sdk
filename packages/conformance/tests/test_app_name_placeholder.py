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


def test_o005_silent_on_prefixed_prose_constant() -> None:
    """A prose *suffix* reads as message text even with a qualifier prefix."""
    assert "O005" not in _o_ids(
        'START_MESSAGE = "the {app_name} token was left unresolved"\n'
    )


# ── ...but the exclusions must stay narrow ───────────────────────────────────


def test_o005_fires_on_all_caps_queue_template() -> None:
    """An ALL_CAPS name is not a free pass: this one is a genuine queue template,
    neither bare-token nor prose-named, so the sentinel exclusion must not
    swallow it."""
    assert "O005" in _o_ids('TASK_QUEUE = "atlan-{app_name}-prod"\n')


def test_o005_fires_on_prose_fragment_queue_templates() -> None:
    """A queue template whose name merely *contains* a prose fragment is a
    dispatch template, not message text: the prose exclusion matches the
    trailing delimited segment, never a substring."""
    for name in ("MESSAGE_QUEUE", "HELP_QUEUE", "DOC_QUEUE"):
        assert "O005" in _o_ids(f'{name} = "atlan-{{app_name}}-prod"\n'), name


def test_o005_fires_on_escaped_brace_fstring() -> None:
    """``f"{{app_name}}"`` is *not* interpolated: the escaped braces evaluate to
    the literal runtime text ``{app_name}``, which freezes into the identifier
    exactly like a plain literal — the precise dangerous shape."""
    src = 'task_queue = f"atlan-{{app_name}}-production"\n'
    assert "O005" in _o_ids(src)


def test_o005_fires_on_escaped_brace_fstring_call_argument() -> None:
    assert "O005" in _o_ids('register_queue(f"atlan-{{app_name}}-production")\n')


def test_o005_silent_on_interpolated_fstring_in_diagnostic() -> None:
    """A *resolving* f-string quoting the token inside a log call is still a
    diagnostic, not a dispatch — the f-string waiver must not re-flag it."""
    assert "O005" not in _o_ids('logger.info(f"queue atlan-{app_name}-prod")\n')


def test_o005_silent_on_escaped_brace_fstring_in_diagnostic() -> None:
    """Double-brace-quoting the token is a normal way to make it render
    literally inside an f-string log message; still not a dispatch."""
    assert "O005" not in _o_ids('logger.info(f"queue atlan-{{app_name}}-prod")\n')


def test_o005_silent_on_escaped_brace_fstring_docstring() -> None:
    """An f-string docstring quoting the token with escaped braces is
    documentation (``Expr(JoinedStr)``), not a value that can dispatch."""
    assert "O005" not in _o_ids(
        'f"""Queue names look like atlan-{{app_name}}-prod."""\n'
    )


def test_o005_escaped_brace_fstring_suppressed_inline() -> None:
    src = 'task_queue = f"atlan-{{app_name}}-prod"  # conformance: ignore[O005] resolved by caller\n'
    assert "O005" not in _o_ids(src)


# ── The escaped-brace waiver must not re-flag an already-resolved sibling ────
#
# The waiver re-flags only the escaped-brace f-string's own token-bearing
# pieces — never an unrelated exempt literal that happens to share the
# physical line (round-2 review nit).


def _o_count(src: str) -> int:
    return sum(
        1 for f in o_scan(src, "app/x.py") if f.rule_id == "O005" and not f.suppressed
    )


def test_o005_sibling_resolved_format_receiver_not_reflagged() -> None:
    """The `.format(app_name=...)` receiver on the same line as an escaped-brace
    f-string is already resolved — only the f-string may be flagged."""
    src = 'x = "atlan-{app_name}".format(app_name=a); y = f"{{app_name}}"\n'
    assert _o_count(src) == 1


def test_o005_sibling_diagnostic_literal_not_reflagged() -> None:
    """A diagnostic message sharing a line with an escaped-brace f-string keeps
    its diagnostic exemption — only the f-string may be flagged."""
    src = 'logger.error("unresolved {app_name}"); y = f"{{app_name}}"\n'
    assert _o_count(src) == 1


def test_o005_silent_on_format_call_on_fstring_receiver() -> None:
    """``f"{{app_name}}".format(app_name=a)`` resolves at runtime exactly like a
    plain-literal receiver — the f-string parse shape is not a reason to flag
    a site that *does* substitute the token."""
    assert "O005" not in _o_ids(
        'task_queue = f"atlan-{{app_name}}".format(app_name=a)\n'
    )


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
