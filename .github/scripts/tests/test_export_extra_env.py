"""Tests for .github/scripts/export_extra_env.py."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "export_extra_env.py"
_spec = importlib.util.spec_from_file_location("export_extra_env", _MODULE_PATH)
assert _spec and _spec.loader
export_extra_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_extra_env)

render = export_extra_env.render
render_masks = export_extra_env.render_masks
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


# ── Log masking (--mask-only) ────────────────────────────────────────────────
# The defect this section pins: the caller registers the composed
# E2E_SOURCE_ENV_JSON blob as a secret, but the runner's masker replaces
# registered *strings* and does not match substrings of one — so every value
# extracted out of the blob was printed in cleartext by the next step that
# rendered its `env:` group. Each value has to be registered in its own right.
#
# Synthetic fixtures only. No shape below is a real credential.


def _registered_secrets(mask_output: str) -> list[str]:
    """Decode ``--mask-only`` output the way the runner reads workflow commands.

    Mirrors what the script itself is responsible for: each line is one
    ``::add-mask::`` command whose data is unescaped the way the runner's
    ``UnescapeData`` does it (``%0D``/``%0A`` before ``%25``, so a value that
    literally contained the text ``%0A`` is not turned into a newline).

    Deliberately does NOT reproduce the extra registrations current
    ``AddMaskCommandExtension`` versions add on their own (it splits the data on
    CR/LF and registers each trimmed piece). Modelling only the documented
    contract is the point: the assertions below then hold on a runner that does
    no splitting, which is what makes this fix version-independent.

    Blank data is refused by the runner with a warning ("Can't add secret mask
    for empty string"), so this asserts none is ever emitted.
    """
    secrets: list[str] = []
    for line in mask_output.splitlines():
        assert line.startswith("::add-mask::"), f"not a mask command: {line!r}"
        data = line[len("::add-mask::") :]
        assert data.strip(), "runner refuses ::add-mask:: with blank data"
        secrets.append(
            data.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")
        )
    return secrets


def _redact(log: str, secrets: list[str]) -> str:
    """Redact *log* the way the runner's masker does: per line, per secret.

    The line-orientation is the whole point. ``SecretMasker.ReplaceSecrets`` is
    handed log output a line at a time, so a registered string that spans
    newlines can never match anything it sees.
    """
    out = []
    for line in log.splitlines():
        for secret in secrets:
            line = line.replace(secret, "***")
        out.append(line)
    return "\n".join(out)


# A PEM-shaped multi-line value: the case that motivated the JSON input in the
# first place, and the case whole-value masking alone does not cover.
_FAKE_PEM = (
    "-----BEGIN TEST KEY-----\n"
    "bm90LWEtcmVhbC1rZXktMDAwMQ\n"
    "bm90LWEtcmVhbC1rZXktMDAwMg\n"
    "-----END TEST KEY-----"
)


def test_mask_only_emits_one_command_per_single_line_value():
    out = render_masks('{"E2E_HOST": "db.invalid", "E2E_PORT": "1433"}')
    assert _registered_secrets(out) == ["db.invalid", "1433"]


def test_mask_only_emits_no_github_env_lines():
    """The two modes must not bleed into each other.

    `--mask-only` output goes to the step's stdout (the log). If it also carried
    heredoc headers, a caller that redirected it would corrupt $GITHUB_ENV; if
    the env-writing mode carried mask commands, they would be written into the
    env file as garbage instead of reaching the runner.
    """
    payload = '{"A": "x", "KEY": %s}' % json.dumps(_FAKE_PEM)
    assert "<<" not in render_masks(payload)
    assert "::add-mask::" not in render(payload)


def test_every_value_is_masked_even_the_harmless_looking_ones():
    """The script cannot tell which keys hold secrets, so it does not guess."""
    payload = '{"E2E_HOST": "db.invalid", "E2E_PORT": "1433", "E2E_USER": "svc"}'
    assert len(_registered_secrets(render_masks(payload))) == 3


@pytest.mark.parametrize("empty", ['""', "null", '"   "', '"\\n\\n"'])
def test_empty_values_are_skipped(empty: str):
    """`::add-mask::` with blank data makes the runner warn and register nothing.

    A mask of whitespace would also match every space in the log, so these are
    dropped rather than emitted.
    """
    assert render_masks('{"MISSING": %s}' % empty) == ""


def test_empty_payload_masks_nothing():
    assert render_masks("") == ""
    assert render_masks("   \n ") == ""


def test_blank_lines_inside_a_multiline_value_are_skipped():
    """A pretty-printed or blank-line-separated value must not emit blank masks."""
    value = "first\n\n   \nlast"
    secrets = _registered_secrets(render_masks('{"K": %s}' % json.dumps(value)))
    assert secrets == [value, "first", "last"]


def test_indented_lines_are_masked_indented_and_stripped():
    """A pretty-printed service-account JSON is the shape that needs both forms.

    The indented line matches the value as it sits in the log; the stripped line
    matches when the same line is re-emitted unindented (and is what the runner's
    own `TrimEntries` split would have registered, on the versions that have it).
    """
    value = '{\n  "private_key_id": "abc123"\n}'
    secrets = _registered_secrets(render_masks('{"SA": %s}' % json.dumps(value)))
    assert '  "private_key_id": "abc123"' in secrets
    assert '"private_key_id": "abc123"' in secrets
    assert _redact('  "private_key_id": "abc123"', secrets).strip() == "***"


def test_multiline_value_is_masked_whole_and_per_line():
    """The behaviour this fix turns on, and why it is not a single command.

    A multi-line secret needs its lines registered individually: the masker is
    handed log output a line at a time, so the whole-value registration alone
    matches nothing when the value is echoed across several lines.
    """
    secrets = _registered_secrets(render_masks('{"KEY": %s}' % json.dumps(_FAKE_PEM)))

    assert _FAKE_PEM in secrets, "whole value must still be registered"
    for line in _FAKE_PEM.splitlines():
        assert line in secrets, f"line not registered, would leak: {line[:12]}..."

    # What the runner would print for `echo "$KEY"`, masking only what this
    # script registered.
    redacted = _redact(f"KEY: {_FAKE_PEM}", secrets)
    assert "TEST KEY" not in redacted
    assert "not-a-real-key" not in redacted.replace("-", "")
    for line in _FAKE_PEM.splitlines():
        assert line not in redacted


def test_whole_value_masking_alone_would_not_redact_a_multiline_value():
    """Negative control: proves the per-line commands are what does the work.

    Registering only the whole multi-line value — the intuitive single
    `::add-mask::` — leaves every line of the PEM intact, because the masker
    never sees a line containing the whole thing.

    Current runners paper over this by splitting `add-mask` data on CR/LF
    themselves, so shipping only the whole-value command would pass on
    github.com today. That split is not part of the documented `add-mask`
    contract and older self-hosted/GHES runners predate it, which is why the
    lines are emitted here instead of assumed.
    """
    leaked = _redact(f"KEY: {_FAKE_PEM}", [_FAKE_PEM])
    assert leaked == f"KEY: {_FAKE_PEM}"
    for line in _FAKE_PEM.splitlines():
        assert line in leaked


def test_multiline_value_survives_masking_as_a_single_escaped_command():
    """The whole-value command is one line, so it must be %0A-escaped.

    An unescaped newline would split one `::add-mask::` into a command plus
    stray log lines — and those stray lines would be the secret, printed.
    """
    out = render_masks('{"KEY": %s}' % json.dumps(_FAKE_PEM))
    first = out.splitlines()[0]
    assert first == "::add-mask::" + _FAKE_PEM.replace("\n", "%0A")
    assert _registered_secrets(out)[0] == _FAKE_PEM


def test_crlf_value_is_masked_per_line():
    """A secret pasted from Windows arrives CRLF; \\r must not split the command."""
    value = "alpha\r\nbeta"
    out = render_masks('{"K": %s}' % json.dumps(value))
    assert out.splitlines()[0] == "::add-mask::alpha%0D%0Abeta"
    secrets = _registered_secrets(out)
    assert secrets[0] == value
    assert "alpha" in secrets and "beta" in secrets


def test_percent_in_a_value_round_trips():
    """`%` must be escaped first, or the runner unescapes the markers we added."""
    assert _registered_secrets(render_masks('{"K": "p@ss%word"}')) == ["p@ss%word"]


def test_value_containing_literal_percent_0a_is_not_turned_into_a_newline():
    """Without the `%` -> `%25` pass this would register the wrong string.

    A password containing the text `%0A` would be unescaped by the runner into a
    newline, the real value would never match, and it would leak.
    """
    assert _registered_secrets(render_masks('{"K": "a%0Ab"}')) == ["a%0Ab"]


def test_duplicate_mask_candidates_are_emitted_once():
    """A single-line value would otherwise be masked twice (whole == its line)."""
    assert render_masks('{"K": "solo"}') == "::add-mask::solo\n"
    repeated = "same\nsame"
    assert _registered_secrets(render_masks('{"K": %s}' % json.dumps(repeated))) == [
        repeated,
        "same",
    ]


def test_scalars_are_masked_as_their_string_form():
    assert _registered_secrets(render_masks('{"A": 1, "B": 2.5, "C": true}')) == [
        "1",
        "2.5",
        "True",
    ]


@pytest.mark.parametrize(
    "payload, match",
    [
        ("E2E_HOST=db.invalid", "not valid JSON"),
        ('["E2E_HOST"]', "must be a JSON object"),
        ('{"A": {"nested": 1}}', "must be a scalar"),
        ('{"A-B": "v"}', "not a valid environment variable"),
    ],
)
def test_mask_mode_rejects_what_env_mode_rejects(payload: str, match: str):
    """Validation is shared, and the mask pass runs first.

    So a payload the env pass would refuse fails the step *before* anything
    reaches $GITHUB_ENV, and the two passes can never disagree about which
    values exist — a value masked but not written is harmless, a value written
    but not masked is the bug.
    """
    with pytest.raises(ExtraEnvError, match=match):
        render_masks(payload)
    with pytest.raises(ExtraEnvError, match=match):
        render(payload)


def test_error_messages_never_contain_the_value():
    """Errors are printed to the log; the value is a credential."""
    secret = "s3cr3t-not-real"
    with pytest.raises(ExtraEnvError) as excinfo:
        render_masks('{"A-B": %s}' % json.dumps(secret))
    assert secret not in str(excinfo.value)


# ── Env-writing mode is unchanged ────────────────────────────────────────────


def test_env_mode_output_is_unchanged_by_the_mask_refactor():
    """The heredoc contract callers depend on, pinned against the parse split."""
    payload = '{"E2E_HOST": "db.invalid", "KEY": %s}' % json.dumps(_FAKE_PEM)
    out = render(payload)
    assert _parse(out) == {"E2E_HOST": "db.invalid", "KEY": _FAKE_PEM}
    assert out.endswith("\n")
    assert len(re.findall(r"<<(__EXTRA_ENV_[0-9a-f]+__)", out)) == 2


# ── CLI wiring ───────────────────────────────────────────────────────────────


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_MODULE_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_mask_only_flag_selects_mask_output():
    payload = '{"KEY": %s}' % json.dumps(_FAKE_PEM)
    masks = _run("--json", payload, "--mask-only")
    assert masks.returncode == 0
    assert _registered_secrets(masks.stdout)[0] == _FAKE_PEM
    assert "<<" not in masks.stdout


def test_cli_without_the_flag_still_writes_env_lines_only():
    payload = '{"KEY": %s}' % json.dumps(_FAKE_PEM)
    env = _run("--json", payload)
    assert env.returncode == 0
    assert "::add-mask::" not in env.stdout
    assert _parse(env.stdout) == {"KEY": _FAKE_PEM}


def test_cli_mask_mode_fails_closed_on_a_bad_payload():
    """Non-zero here aborts the step under `bash -e`, before the env write."""
    bad = _run("--json", "not json", "--mask-only")
    assert bad.returncode == 1
    assert "::error::" in bad.stderr
    assert bad.stdout == ""


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


# ── Every call site masks before it writes ───────────────────────────────────
# The ordering guarantee lives in the workflow YAML, not in the script, so this
# is where it gets regression-tested. A call site that kept only the
# `>> "$GITHUB_ENV"` line — the shape that shipped originally, and the easy thing
# to copy-paste into a new caller — would put unregistered credentials into the
# environment of every later step. Call sites are discovered rather than listed,
# so a new one is covered the day it is added.

# `python3 <path>/export_extra_env.py \` + `--json "$VAR" <mode>`.
_CALL_SITE_INVOCATION = re.compile(
    r"export_extra_env\.py\"?[ \t]*\\\n"
    r"\s*--json[ \t]+\"(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)\"[ \t]*"
    r"(?P<mode>--mask-only|>>[ \t]*\"\$GITHUB_ENV\")"
)
_ANY_INVOCATION = re.compile(r"python3[^\n]*export_extra_env\.py")


def _call_site_files() -> list[Path]:
    workflow_dir = _REPO_ROOT / ".github"
    found = [
        path
        for path in sorted(workflow_dir.rglob("*.y*ml"))
        if "export_extra_env.py" in path.read_text()
    ]
    assert found, "no call site found — did the script move or lose its callers?"
    return found


def test_call_sites_are_the_expected_files():
    """Fails loudly if a call site appears somewhere this PR did not audit."""
    relative = {p.relative_to(_REPO_ROOT).as_posix() for p in _call_site_files()}
    assert relative == {
        ".github/actions/connector-integration-tests/action.yaml",
        ".github/actions/sdr-e2e/action.yaml",
        ".github/workflows/tests-reusable.yaml",
    }


def test_every_env_write_call_site_masks_first():
    for path in _call_site_files():
        text = path.read_text()
        where = path.relative_to(_REPO_ROOT).as_posix()

        calls = list(_CALL_SITE_INVOCATION.finditer(text))
        assert len(calls) == len(
            _ANY_INVOCATION.findall(text)
        ), f"{where} invokes the script in a shape this test cannot check"

        modes = ["mask" if c["mode"] == "--mask-only" else "write" for c in calls]
        assert modes, f"{where} has no recognisable invocation"
        assert len(modes) % 2 == 0, f"{where} has an unpaired invocation: {modes}"
        assert modes == ["mask", "write"] * (len(modes) // 2), (
            f"{where} does not mask before writing $GITHUB_ENV: {modes}. Values "
            "written to $GITHUB_ENV before ::add-mask:: registers them leak in "
            "cleartext from the next step that renders its env: group."
        )
        for mask, write in zip(calls[::2], calls[1::2]):
            assert mask["var"] == write["var"], (
                f"{where} masks {mask['var']} but writes {write['var']} — the "
                "masked payload must be the one written."
            )
