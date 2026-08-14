"""Tests for .github/scripts/export_extra_env.py."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


def _runner_lines(text: str) -> list[str]:
    """Split *text* into lines the way the runner reads a stream: CR/LF only.

    Not `str.splitlines()`, which also breaks on \\v, \\f, \\x1c-\\x1e, \\x85,
    U+2028 and U+2029. The runner does not treat those as line boundaries, so a
    helper that split there would model a stricter runner than the real one and
    would fail on a secret that happens to contain one — the same distinction
    `mask_values` makes when choosing its candidates.

    A single trailing empty element (every emitter here ends with a newline) is
    dropped, matching `splitlines()`.
    """
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


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
    for line in _runner_lines(mask_output):
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
    for line in _runner_lines(log):
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


def test_whole_value_masking_alone_would_not_redact_a_multiline_value(monkeypatch):
    """Negative control: proves the per-line commands are what does the work.

    Reverts `mask_values` to the intuitive single-`::add-mask::` implementation
    and drives the real `render_masks` with it, so this fails if the per-line
    candidates ever stop being emitted — which a hand-built `_redact` call could
    not detect. Every line of the PEM survives, because the masker never sees a
    line containing the whole value.

    Current runners paper over this by splitting `add-mask` data on CR/LF
    themselves, so shipping only the whole-value command would pass on
    github.com today. That split is not part of the documented `add-mask`
    contract and older self-hosted/GHES runners predate it, which is why the
    lines are emitted here instead of assumed.
    """
    monkeypatch.setattr(export_extra_env, "mask_values", lambda text: [text])
    secrets = _registered_secrets(render_masks('{"KEY": %s}' % json.dumps(_FAKE_PEM)))

    assert secrets == [_FAKE_PEM], "whole-value-only is the scenario under test"
    for line in _FAKE_PEM.splitlines():
        assert line not in secrets, "per-line registration must be what is missing"

    leaked = _redact(f"KEY: {_FAKE_PEM}", secrets)
    assert leaked == f"KEY: {_FAKE_PEM}", "nothing is redacted"
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


def test_padded_single_line_value_is_masked_padded_and_stripped():
    """A single-line value gets its stripped form too, not just multi-line lines.

    A secret pasted with surrounding whitespace is a different string to the
    masker in each form, so both are registered. Current runners would trim it
    themselves via their own `add-mask` split — relying on that would put the
    single-line path back on the runner version the multi-line path deliberately
    does not depend on.
    """
    secrets = _registered_secrets(render_masks('{"TOKEN": "  tok3n-not-real  "}'))

    assert secrets == ["  tok3n-not-real  ", "tok3n-not-real"]
    assert _redact("TOKEN=tok3n-not-real", secrets) == "TOKEN=***"


def test_separators_python_splits_but_the_runner_does_not_are_left_alone():
    """Lines are split on CR/LF only, matching what the runner calls a line.

    `str.splitlines()` also breaks on \\v, \\f, \\x1c-\\x1e, \\x85, U+2028 and
    U+2029. Splitting there would register fragments the runner never treats as
    lines, and those short fragments would redact unrelated log text.
    """
    value = "alpha\x0bbeta gamma"
    secrets = _registered_secrets(render_masks('{"K": %s}' % json.dumps(value)))

    assert secrets == [value], "no CR/LF present, so there is nothing to split"
    for fragment in ("alpha", "beta", "gamma"):
        assert fragment not in secrets


def test_mixed_separators_split_only_at_real_line_breaks():
    """A \\v inside a line travels with that line rather than starting a new one."""
    value = "one\ntwo\x0cthree"
    secrets = _registered_secrets(render_masks('{"K": %s}' % json.dumps(value)))

    assert "two\x0cthree" in secrets
    assert "three" not in secrets


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
        r"^  extra-env:", action_yaml, re.MULTILINE
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
#
# Discovery only helps if the suite actually runs on the files it discovers, so
# `test_the_suite_runs_on_every_file_these_guards_read` pins the trigger too.

# `<anything>/export_extra_env.py \` + `--json "$VAR" <mode>`. Deliberately says
# nothing about the interpreter: `python3 x.py`, `python x.py` and
# `uv run python x.py` are all the same call as far as ordering goes.
_CALL_SITE_INVOCATION = re.compile(
    r"export_extra_env\.py\"?[ \t]*\\\n"
    r"\s*--json[ \t]+\"(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)\"[ \t]*"
    r"(?P<mode>--mask-only|>>[ \t]*\"\$GITHUB_ENV\")"
)

# Discovery and the parity count below both key on the module name, with no
# interpreter and no `.py` required. Anything narrower fails in the dangerous
# direction — see `_call_site_files`.
_SCRIPT_MENTION = "export_extra_env"

# File types under .github/ that can carry a `run:` payload. Not YAML only: both
# composites already delegate `run:` logic to sibling shell scripts
# (`repin-application-sdk.sh`, `with-retry.sh`), and the CI standard in
# docs/standards/ci.md actively pushes new logic out of workflow YAML and into
# .github/scripts/. A caller that lands in a .sh or .py helper has to be
# discovered there or the guarantee is YAML-only. `*.py` does not match `.pyc`,
# so __pycache__ stays out.
_CALL_SITE_SUFFIXES = ("*.y*ml", "*.sh", "*.py")

# Files under .github/ that name the script in prose without running it. Kept as
# a named exclusion rather than encoded into the predicate, and validated below:
# if a mention here ever appears outside a comment, the exclusion stops applying
# and the file is audited like any other caller.
_PROSE_ONLY_FILES = {
    ".github/workflows/scripts-tests.yaml",
    # FND-31. Names this script in a comment only, pointing at it as the
    # explanation for why its own tenant-resolution step is two passes. It never
    # invokes it. The "no live mention" assertion below is what keeps that true:
    # if it ever starts invoking the script, it stops being excluded.
    ".github/workflows/e2e-tenant-install.yaml",
}

# The script and its own tests are the implementation, not callers of it. Both
# contain literal `>> "$GITHUB_ENV"` invocations — in the module docstring's usage
# example and in this file's parametrized fixtures — which are not comments and so
# would be audited as unmasked call sites once the glob reaches .py files.
#
# resolve_e2e_tenant.py is the third: it `import`s this module in-process to reuse
# `render`/`render_masks` rather than shelling out to it, so the ordering guarantee
# for its own callers is enforced by its own two-mode CLI — audited by the twin
# guard in test_resolve_e2e_tenant.py, which discovers *its* call sites the same
# way this one does. Auditing it here would look for a shell invocation that does
# not exist, and the `>> "$GITHUB_ENV"` lines in its docstring and its tests would
# read as unmasked writes.
#
# fetch_dataforge_source.py is the fourth and fifth (with its tests): it never
# invokes this script — its call sites do, and those are YAML files this guard
# audits — but its module docstring documents the required call-site shape with
# a literal `--json … --mask-only` / `>> "$GITHUB_ENV"` pair, and its test file
# pins the same shape in regex fixtures. Both would otherwise be audited as
# call sites. The capture-side property those call sites add (fetch stdout into
# a shell variable, never the log) is guarded by test_fetch_dataforge_source's
# own call-site tests, the same twin-guard split as resolve_e2e_tenant.
#
# Computed rather than spelled out: the paths cannot drift, and the exclusion
# cannot quietly widen to cover some further file.
_RESOLVE_TENANT_MODULE = _MODULE_PATH.parent / "resolve_e2e_tenant.py"
_RESOLVE_TENANT_TESTS = Path(__file__).parent / "test_resolve_e2e_tenant.py"
_FETCH_DATAFORGE_MODULE = _MODULE_PATH.parent / "fetch_dataforge_source.py"
_FETCH_DATAFORGE_TESTS = Path(__file__).parent / "test_fetch_dataforge_source.py"
_NOT_A_CALLER = frozenset(
    {
        _MODULE_PATH.resolve(),
        Path(__file__).resolve(),
        _RESOLVE_TENANT_MODULE.resolve(),
        _RESOLVE_TENANT_TESTS.resolve(),
        _FETCH_DATAFORGE_MODULE.resolve(),
        _FETCH_DATAFORGE_TESTS.resolve(),
    }
)


def _live_mentions(text: str) -> list[str]:
    """Lines naming the script that are not comments.

    Splits on ``\\n`` directly rather than via ``_runner_lines()``. The two agree
    today — ``Path.read_text()`` has already normalised line endings — but
    ``_runner_lines`` exists to model the runner's *log-stream* semantics, and a
    future edit to it made for a masking reason should not silently change which
    files count as call sites. ``#`` is the comment marker in all three of the
    file types scanned (YAML, shell, Python).
    """
    return [
        line
        for line in text.split("\n")
        if _SCRIPT_MENTION in line and not line.lstrip().startswith("#")
    ]


def _call_site_files() -> list[Path]:
    """Every file under ``.github/`` that could run the script.

    The predicate is deliberately the **broadest** thing that works: any
    non-comment line naming ``export_extra_env``, with no interpreter and no
    ``.py`` required, in any file type that can carry a ``run:`` payload. The two
    failure directions are not symmetric:

    * A false positive — prose mistaken for a call site — fails **loud and
      safe**. It trips the ordering assertion below, which is how
      scripts-tests.yaml's own header comment surfaced.
    * A false negative — a real caller the predicate does not see — fails
      **green and open**. It is never audited, so a caller that writes
      ``$GITHUB_ENV`` with no ``--mask-only`` reintroduces the credential leak
      with every guard passing.

    Two things follow. It must not require ``python3`` on the same line as the
    filename, which would miss ``uv run python …/export_extra_env.py`` (the shape
    scripts-tests.yaml itself uses to run pytest), ``python …`` without the ``3``,
    ``python3 -m export_extra_env`` (no ``.py`` at all), and a two-line
    ``SCRIPT="…/export_extra_env.py"`` + ``python3 "$SCRIPT"`` indirection. And it
    must not scan YAML only: a composite whose ``action.yaml`` says
    ``run: ${{ github.action_path }}/leak.sh`` puts the invocation in a file no
    YAML glob opens, which is the direction this repo's own CI standard pushes new
    shell logic.

    Two exclusions, both narrower than the predicate and both validated so they
    cannot rot into holes:

    * ``_PROSE_ONLY_FILES`` — names the script in a comment only. Asserted to have
      no live mention, so an excluded file that starts invoking it stops being
      excluded.
    * ``_NOT_A_CALLER`` — the script itself and this test file. Computed from
      ``_MODULE_PATH`` and ``__file__``, so it cannot drift or widen.
    """
    github_dir = _REPO_ROOT / ".github"
    found: list[Path] = []
    for pattern in _CALL_SITE_SUFFIXES:
        for path in github_dir.rglob(pattern):
            text = path.read_text()
            if _SCRIPT_MENTION not in text:
                continue
            if path.resolve() in _NOT_A_CALLER:
                continue
            where = path.relative_to(_REPO_ROOT).as_posix()
            if where in _PROSE_ONLY_FILES:
                live = _live_mentions(text)
                assert not live, (
                    f"{where} is on the prose-only exclusion list but now names "
                    f"the script outside a comment: {live}. If it genuinely "
                    "invokes the script, drop it from _PROSE_ONLY_FILES so it is "
                    "audited like any other call site."
                )
                continue
            found.append(path)
    assert found, "no call site found — did the script move or lose its callers?"
    return sorted(set(found))


def test_call_sites_are_the_expected_files():
    """Fails loudly if a call site appears somewhere this PR did not audit."""
    relative = {p.relative_to(_REPO_ROOT).as_posix() for p in _call_site_files()}
    assert relative == {
        ".github/actions/connector-integration-tests/action.yaml",
        ".github/actions/sdr-e2e/action.yaml",
        ".github/workflows/tests-reusable.yaml",
    }


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(
            '        python3 "$D/export_extra_env.py" \\\n'
            '          --json "$P" >> "$GITHUB_ENV"',
            id="python3",
        ),
        pytest.param(
            '        python "$D/export_extra_env.py" \\\n'
            '          --json "$P" >> "$GITHUB_ENV"',
            id="python-without-the-3",
        ),
        pytest.param(
            '        uv run python "$D/export_extra_env.py" \\\n'
            '          --json "$P" >> "$GITHUB_ENV"',
            id="uv-run-python",
        ),
        pytest.param(
            '        python3 -m export_extra_env --json "$P" >> "$GITHUB_ENV"',
            id="dash-m-no-dot-py",
        ),
        pytest.param(
            '        SCRIPT="$D/export_extra_env.py"\n'
            '        python3 "$SCRIPT" --json "$P" >> "$GITHUB_ENV"',
            id="indirect-via-shell-var",
        ),
        pytest.param(
            '        python3 "$D/export_extra_env.py" --json "$P" >> "$GITHUB_ENV"',
            id="single-line-no-continuation",
        ),
    ],
)
def test_an_unmasked_caller_is_never_discovered_green(tmp_path, monkeypatch, shape):
    """Every plausible spelling of a write-only caller must fail, not pass.

    Each shape writes `$GITHUB_ENV` with no `--mask-only` — i.e. reintroduces the
    credential leak — and must be both discovered and rejected.

    Four of the six (`python-without-the-3`, `uv-run-python`, `dash-m-no-dot-py`,
    `indirect-via-shell-var`) evaded the earlier predicate, which required the
    literal `python3` on the same line as `export_extra_env.py`; they are the
    evidence for keeping discovery interpreter-agnostic. The other two
    (`python3`, `single-line-no-continuation`) matched it too and are baseline
    coverage, not evidence of the widening — they are here so the happy path and
    the unparseable-single-line path stay pinned alongside the rest.

    The failure direction matters more than the message: a shape the assertions
    cannot parse must fail *loudly* (unreadable-line parity), never be skipped.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "new-caller.yaml").write_text(
        "jobs:\n  leak:\n    steps:\n      - run: |\n" + shape + "\n"
    )
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)

    discovered = {p.name for p in _call_site_files()}
    assert discovered == {"new-caller.yaml"}, "the caller must be discovered"

    with pytest.raises(AssertionError):
        _assert_call_sites_mask_first()


@pytest.mark.parametrize("helper", ["leak.sh", "leak.py"])
def test_a_caller_hiding_in_a_non_yaml_helper_is_discovered(
    tmp_path, monkeypatch, helper
):
    """Discovery must not be YAML-only.

    Both composites already delegate `run:` logic to sibling shell scripts, and
    the CI standard pushes new logic into `.github/scripts/`. A composite whose
    `action.yaml` only says `run: <path>/leak.sh`, with the unmasked invocation
    inside that script, was never opened by a `*.y*ml` glob — so it was never
    audited and both guards passed green on a reintroduced leak.
    """
    action_dir = tmp_path / ".github" / "actions" / "sneaky"
    action_dir.mkdir(parents=True)
    (action_dir / "action.yaml").write_text(
        "runs:\n  using: composite\n  steps:\n"
        "    - shell: bash\n"
        f"      run: ${{{{ github.action_path }}}}/{helper}\n"
    )
    (action_dir / helper).write_text(
        '#!/usr/bin/env bash\npython3 "$D/export_extra_env.py" \\\n'
        '  --json "$P" >> "$GITHUB_ENV"\n'
    )
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)

    assert {p.name for p in _call_site_files()} == {helper}, (
        f"{helper} carries the invocation and must be discovered; the action.yaml "
        "that merely launches it never names the script"
    )
    # A lone write is an odd number of invocations, so it trips the unpaired
    # assertion before the ordering one — either way the step is rejected.
    with pytest.raises(AssertionError, match="unpaired invocation"):
        _assert_call_sites_mask_first()


def test_the_script_and_its_own_tests_are_not_treated_as_callers(monkeypatch):
    """`_NOT_A_CALLER` keeps the glob widening from auditing the implementation.

    Both files name the script on non-comment lines — the module docstring's usage
    example and this file's parametrized fixtures both contain a literal
    `>> "$GITHUB_ENV"` — so once discovery reaches `.py` they would be audited as
    unmasked call sites. Asserted here because the exclusion is what makes the
    widened glob usable at all.
    """
    assert _MODULE_PATH.resolve() in _NOT_A_CALLER
    assert Path(__file__).resolve() in _NOT_A_CALLER

    # Both really do carry live mentions — so the exclusion is load-bearing, not
    # a no-op that happens to look necessary.
    for path in (_MODULE_PATH, Path(__file__)):
        assert _live_mentions(path.read_text()), f"{path.name} premise changed"

    discovered = {p.resolve() for p in _call_site_files()}
    assert not (discovered & _NOT_A_CALLER)


def test_prose_naming_the_script_is_not_a_call_site():
    """A comment that names the script must not be audited as if it ran it.

    scripts-tests.yaml's header explains why its trigger is unfiltered, and does
    so by naming this script — so it matches the `.github/**/*.y*ml` glob and
    contains the filename, but runs nothing. Discovering it as a call site makes
    the ordering assertions below fail on a comment, which is a real failure this
    suite hit while the guard keyed on the bare filename. Pinned here so the
    distinction survives the next edit to that comment.
    """
    workflow = _REPO_ROOT / ".github" / "workflows" / "scripts-tests.yaml"
    assert "export_extra_env.py" in workflow.read_text(), "premise of this test"
    assert workflow not in _call_site_files()


def test_a_prose_excluded_file_that_starts_invoking_stops_being_excluded(
    tmp_path, monkeypatch
):
    """The trip-wire that stops `_PROSE_ONLY_FILES` rotting into a hole.

    An exclusion list is itself a place a real caller can hide: add a file for a
    good reason, and every later invocation added to it is skipped silently. So
    membership is conditional on the mention staying inside a comment — the moment
    it does not, the file is audited like any other caller, which for an unmasked
    invocation means the ordering assertion fires.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    excluded = workflows / "scripts-tests.yaml"
    assert (
        excluded.relative_to(tmp_path).as_posix() in _PROSE_ONLY_FILES
    ), "premise: this path is the excluded one"
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)

    # A comment-only mention: excluded, and discovery finds no call site at all.
    excluded.write_text("# runs export_extra_env.py in a comment\njobs: {}\n")
    with pytest.raises(AssertionError, match="no call site found"):
        _call_site_files()

    # The same file with a live invocation: the exclusion no longer applies.
    excluded.write_text(
        "jobs:\n  x:\n    steps:\n      - run: |\n          python3 \\\n"
        '            "$D/export_extra_env.py" --json "$P" >> "$GITHUB_ENV"\n'
    )
    with pytest.raises(AssertionError, match="prose-only exclusion list"):
        _call_site_files()


def test_the_suite_runs_on_every_file_these_guards_read():
    """A `paths:` filter on this suite's workflow would silently disable it.

    The guards above read YAML outside `.github/scripts/`, but the workflow that
    runs them originally filtered on `paths: [.github/scripts/**, ...]` — which
    matches none of the call-site files. A PR dropping `--mask-only` from a call
    site therefore triggered no run, the guard never executed, and CI went green
    on a reintroduced credential leak. Enumerating the call sites in `paths:`
    would only move the hole one file along, since the point of discovery is that
    the list is not maintained by hand.

    So: assert the trigger has no path filter at all. Cheap for a sub-minute
    suite, and it fails on the change that would blind it.
    """
    workflow = _REPO_ROOT / ".github" / "workflows" / "scripts-tests.yaml"
    parsed = yaml.safe_load(workflow.read_text())
    # YAML 1.1 resolves a bare `on` key to the boolean True, so it is not
    # reachable under the string "on" that the file appears to spell.
    on_block = parsed["on"] if "on" in parsed else parsed[True]

    assert "pull_request" in on_block, "the suite must run on pull_request"
    triggers = on_block["pull_request"] or {}
    for key in ("paths", "paths-ignore"):
        assert key not in triggers, (
            f"scripts-tests.yaml declares `{key}`, so this suite no longer runs "
            "on every PR. The call-site guards read YAML outside "
            ".github/scripts/ and would stop firing on the very edits they "
            "exist to catch. See the header comment in that workflow."
        )

    # And the suite it runs is the one this file lives in.
    assert "pytest .github/scripts/tests" in workflow.read_text()


def _assert_call_sites_mask_first() -> None:
    """The ordering audit, as a plain helper.

    Extracted so `test_an_unmasked_caller_is_never_discovered_green` can assert it
    raises without calling a test function directly — that coupling would turn a
    fixture added to the test into a `TypeError` where an `AssertionError` was
    expected.
    """
    for path in _call_site_files():
        text = path.read_text()
        where = path.relative_to(_REPO_ROOT).as_posix()

        # Parity, not just presence: every non-comment mention of the script has
        # to belong to an invocation the ordering assertions below can read. Each
        # recognised call names the script exactly once, so the two counts match
        # only when nothing is invoked in an unparseable shape — a single-line
        # invocation, an indirection through a shell variable, or a `-m` form.
        # This is what keeps a file that is *discovered* from passing silently.
        calls = list(_CALL_SITE_INVOCATION.finditer(text))
        live = _live_mentions(text)
        assert len(calls) == len(live), (
            f"{where} names the script on {len(live)} non-comment line(s) but "
            f"only {len(calls)} match a checkable invocation. Rewrite it in the "
            'two-line `--json "$VAR" <mode>` shape, or teach '
            f"_CALL_SITE_INVOCATION the new shape. Unreadable lines: {live}"
        )

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


def test_every_env_write_call_site_masks_first():
    _assert_call_sites_mask_first()
