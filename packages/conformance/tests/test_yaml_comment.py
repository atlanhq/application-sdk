"""Tests for the one YAML inline-comment reader the line-based YAML checks share.

Five checks used to strip comments five ways (FND-4946): quote-aware, first
``" #"``, and first bare ``#``. The rule-level tests below are the inputs
where the old copies gave the wrong answer; each one failed before the checks
moved onto ``strip_yaml_comment``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.suite.checks._ast_common import strip_yaml_comment, yaml_comment_start
from conformance.suite.checks.actions_pinning import iter_uses, scan_text
from conformance.suite.checks.dapr_objectstore import deploy_dapr_false_line
from conformance.suite.checks.e2e_deployment_name import (
    scan_text as scan_deployment_name,
)
from conformance.suite.checks.e2e_workflow_shape import discover, scan_all
from conformance.suite.checks.generated_freshness import _scan_placeholders

_SHA = "df4cb1c069e1874edd31b4311f1884172cec0e10"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("foo # note", "foo"),
        ("foo\t# note", "foo"),
        ("foo", "foo"),
        ("# whole line", ""),
        ("org/repo@ref#frag", "org/repo@ref#frag"),
        ('"a # b"', '"a # b"'),
        ("'a # b'", "'a # b'"),
        ('"a # b" # note', '"a # b"'),
        ("'it''s # here' # note", "'it''s # here'"),
        ('"say \\"hi # there\\"" # note', '"say \\"hi # there\\""'),
        ("Bob's app # note", "Bob's app"),
        ("[a, 'b # c'] # note", "[a, 'b # c']"),
        # A quote inside a plain scalar is text, so the ``#`` after it is a
        # comment (lens F-a5d7dd).
        ('echo "step # 1" && pytest', 'echo "step'),
        ('fixed "label # ${X}', 'fixed "label'),
        # JSON-style flow mapping: the value quote follows ``:`` directly
        # (lens F-a94794).
        ('{"key":"a # b"} # note', '{"key":"a # b"}'),
        ("{'k':'a # b'}", "{'k':'a # b'}"),
        ('extra: {"note":"a # x"}', 'extra: {"note":"a # x"}'),
        ('- run: "echo # 1"', '- run: "echo # 1"'),
        ("key: !!str 'a # b' # c", "key: !!str 'a # b'"),
        ('key: &anc "a # b"', 'key: &anc "a # b"'),
        ("key: a:b # c", "key: a:b"),
        ("key: | # c", "key: |"),
        ("", ""),
    ],
)
def test_strip_yaml_comment(value: str, expected: str) -> None:
    assert strip_yaml_comment(value) == expected


def test_yaml_comment_start_is_line_length_without_a_comment() -> None:
    assert yaml_comment_start("key: value") == len("key: value")
    assert yaml_comment_start("key: value # c") == len("key: value ")


# ── C001 ────────────────────────────────────────────────────────────────────


def test_c001_quoted_ref_with_trailing_comment_is_read_unquoted() -> None:
    """The old stripper skipped quoted values, so the comment stayed attached,
    the quotes could not be removed, and a pinned action read as unpinned."""
    text = f'      - uses: "actions/checkout@{_SHA}" # v6\n'
    assert [u.raw_ref for u in iter_uses(text, "t.yml")] == [f"actions/checkout@{_SHA}"]
    assert scan_text(text, ".github/workflows/t.yml") == []


def test_c001_tab_before_comment_is_a_comment() -> None:
    text = f"      - uses: actions/checkout@{_SHA}\t# v6\n"
    assert scan_text(text, ".github/workflows/t.yml") == []


# ── K029 / K030 ─────────────────────────────────────────────────────────────


def test_k029_tab_before_comment_still_reads_false() -> None:
    text = "deploy:\n  dapr:\n    objectstore: false\t# off for now\n"
    assert deploy_dapr_false_line(text, "objectstore") == 3


def test_k029_quoted_hash_value_is_not_cut() -> None:
    text = 'deploy:\n  dapr:\n    objectstore: "false #1"\n'
    assert deploy_dapr_false_line(text, "objectstore") is None


# ── T021 ────────────────────────────────────────────────────────────────────


def test_t021_quoted_run_value_with_hash_still_names_the_suite(tmp_path: Path) -> None:
    """In a quoted ``run:`` value ``#`` is text; the old stripper cut the line
    at the first ``" #"`` and lost the ``pytest tests/e2e`` after it.

    (Unquoted, ``run: echo "step # 1" && ...`` really is cut at ``#``: the
    quote is inside a plain scalar, so YAML reads the rest as a comment.)
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "bespoke.yaml").write_text(
        "name: Bespoke\non:\n  pull_request:\njobs:\n"
        "  e2e:\n    runs-on: ubuntu-latest\n    steps:\n"
        '      - run: "echo step # 1 && uv run pytest tests/e2e"\n',
        encoding="utf-8",
    )
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    (e2e_dir / "test_thing_full_dag.py").write_text(
        "class TestThingFullDAG:\n    pass\n", encoding="utf-8"
    )
    findings = scan_all(discover(tmp_path), tmp_path)
    assert "T021" not in [f.rule_id for f in findings if not f.suppressed]


# ── T016 ────────────────────────────────────────────────────────────────────


def test_t016_hash_inside_a_value_does_not_hide_the_inherit_marker() -> None:
    """A ``#`` not preceded by whitespace is text, so the value still inherits."""
    text = (
        "services:\n  atlan-app:\n    environment:\n"
        "      ATLAN_DEPLOYMENT_NAME: e2e#${ATLAN_DEPLOYMENT_NAME}\n"
    )
    assert scan_deployment_name(text, "docker-compose.ci.yaml") == []


def test_t016_quote_inside_a_plain_value_does_not_hide_a_hard_code() -> None:
    """The quote is plain-scalar text, so ``# ${...}`` is a comment and the
    value is the hard-coded ``fixed "label`` (lens F-a5d7dd)."""
    text = (
        "services:\n  atlan-app:\n    environment:\n"
        '      ATLAN_DEPLOYMENT_NAME: fixed "label # ${ATLAN_DEPLOYMENT_NAME}\n'
    )
    assert [
        f.rule_id for f in scan_deployment_name(text, "docker-compose.ci.yaml")
    ] == ["T016"]


# ── K009 ────────────────────────────────────────────────────────────────────


def test_k009_placeholder_in_a_quoted_flow_map_value_is_found(tmp_path: Path) -> None:
    """``{"note":"a # {app_name}"}``: the value is quoted, so the placeholder
    is not in a comment (lens F-a107f2)."""
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "app.pkl").write_text("", encoding="utf-8")
    (tmp_path / "atlan.yaml").write_text(
        'extra: {"note":"a # {app_name}"}\n', encoding="utf-8"
    )
    findings = _scan_placeholders(tmp_path, {"contract/app.pkl", "atlan.yaml"})
    assert [f.rule_id for f in findings] == ["K009"]
