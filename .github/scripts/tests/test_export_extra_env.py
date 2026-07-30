"""Tests for .github/scripts/export_extra_env.py."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "export_extra_env.py"
_spec = importlib.util.spec_from_file_location("export_extra_env", _MODULE_PATH)
assert _spec and _spec.loader
export_extra_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_extra_env)

render = export_extra_env.render
ExtraEnvError = export_extra_env.ExtraEnvError


def _parse(rendered: str) -> dict[str, str]:
    """Interpret rendered output the way the runner reads $GITHUB_ENV."""
    env: dict[str, str] = {}
    lines = rendered.splitlines()
    i = 0
    while i < len(lines):
        header = lines[i]
        assert "<<" in header, f"expected heredoc header, got {header!r}"
        name, delimiter = header.split("<<", 1)
        i += 1
        collected: list[str] = []
        while lines[i] != delimiter:
            collected.append(lines[i])
            i += 1
        env[name] = "\n".join(collected)
        i += 1
    return env


def test_empty_input_is_a_noop():
    assert render("") == ""
    assert render("   \n ") == ""


def test_single_line_values_round_trip():
    out = render('{"E2E_HOST": "db.example.com", "E2E_PORT": "1433"}')
    assert _parse(out) == {"E2E_HOST": "db.example.com", "E2E_PORT": "1433"}


def test_multiline_pem_round_trips():
    """The case a KEY=VALUE input cannot express at all."""
    pem = "-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB\n-----END PRIVATE KEY-----"
    out = render('{"KEY": %s}' % __import__("json").dumps(pem))
    assert _parse(out)["KEY"] == pem


def test_scalars_are_stringified():
    out = render('{"A": 1, "B": 2.5, "C": true}')
    assert _parse(out) == {"A": "1", "B": "2.5", "C": "True"}


def test_null_becomes_empty_string():
    """An unset secret reference should not write the literal "None"."""
    assert _parse(render('{"MISSING": null}')) == {"MISSING": ""}


def test_delimiter_is_unique_per_variable():
    out = render('{"A": "1", "B": "2"}')
    delimiters = re.findall(r"<<(__EXTRA_ENV_[0-9a-f]+__)", out)
    assert len(delimiters) == 2
    assert len(set(delimiters)) == 2


def test_value_cannot_terminate_its_own_block():
    """A value containing a delimiter-shaped line must not break out.

    This is the $GITHUB_ENV injection shape: if the value could close the
    heredoc, everything after it would be interpreted as further assignments.
    """
    hostile = "__EXTRA_ENV_deadbeef__\nINJECTED=yes"
    out = render('{"A": %s}' % __import__("json").dumps(hostile))
    assert _parse(out) == {"A": hostile}


def test_invalid_json_is_rejected():
    with pytest.raises(ExtraEnvError, match="not valid JSON"):
        render("E2E_HOST=db.example.com")


def test_non_object_json_is_rejected():
    with pytest.raises(ExtraEnvError, match="must be a JSON object"):
        render('["E2E_HOST"]')


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "A B",
        "A=B",
        "A\nB",
        # The runner splits the heredoc header on the FIRST `<<`, so a name
        # carrying its own `<<` makes it look for the wrong delimiter and the
        # step dies with "Matching delimiter not found".
        "A<<X",
        "A<B",
        # `$` in an anchored regex still matches before a trailing newline, so
        # this shape is the one a `.match()` whitelist would wrongly admit.
        "A\n",
        "A\r",
        # Not POSIX-shaped: a leading digit or a hyphen is not a valid name.
        "1ABC",
        "A-B",
    ],
)
def test_invalid_names_are_rejected(bad: str):
    with pytest.raises(ExtraEnvError):
        render('{%s: "v"}' % __import__("json").dumps(bad))


@pytest.mark.parametrize(
    "good", ["A", "_A", "E2E_HOST", "_", "A1", "SNOWFLAKE_E2E_USER"]
)
def test_posix_shaped_names_are_accepted(good: str):
    assert _parse(render('{%s: "v"}' % __import__("json").dumps(good))) == {good: "v"}


def test_structured_values_are_rejected():
    with pytest.raises(ExtraEnvError, match="must be a scalar"):
        render('{"A": {"nested": 1}}')


# ── Composite-action wiring ──────────────────────────────────────────────────
# Both composites invoke this script through a path relative to
# `github.action_path`. tests-reusable.yaml currently exports the same payload
# inline instead of passing `extra-env` (every action it calls is pinned @main,
# so a new input is invisible until this merges), which leaves the composites'
# own wiring with no live CI caller. Assert it here so the relative path and the
# input declaration are covered by *something*, and so moving either the script
# or an action directory fails loudly.
#
# TODO(follow-up): once these actions can be pinned past this merge, collapse
# the duplication — drop the inline sparse-checkout + export steps from
# tests-reusable.yaml and pass `extra-env: ${{ secrets.E2E_SOURCE_ENV_JSON }}`
# to each composite instead.
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("action", ["connector-integration-tests", "sdr-e2e"])
def test_composite_action_invokes_this_script_at_a_real_path(action: str):
    action_dir = _REPO_ROOT / ".github" / "actions" / action
    action_yaml = (action_dir / "action.yaml").read_text()

    assert re.search(
        r"^  extra-env:", action_yaml, re.M
    ), f"{action}/action.yaml no longer declares the extra-env input"

    match = re.search(
        r"\$\{\{ github\.action_path \}\}/(\S+?export_extra_env\.py)", action_yaml
    )
    assert match, f"{action}/action.yaml no longer invokes export_extra_env.py"

    resolved = (action_dir / match.group(1)).resolve()
    assert (
        resolved == _MODULE_PATH
    ), f"{action} resolves export_extra_env.py to {resolved}, not {_MODULE_PATH}"
    assert resolved.is_file()
