"""Tests for the Release Gate bootstrap vendors into consumer repos.

A release PR may merge only once its full release gate has run AND passed on
its head commit (FND-3839). The verdict is the head commit's `gate-check`
status, posted `pending` by a `/gate-check` comment and settled to
`success`/`failure` when the gated runs finish. The load-bearing properties
are that only `success` passes, that a new head (no status yet) re-blocks,
and that a label can no longer stand in for a verdict.

The script is a bootstrap template rather than an importable module, so
these tests load it from the template directory by path.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import types

import pytest
import yaml

from conformance.bootstrap.render import MANAGED_ACTION_FILES, render

_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "conformance"
    / "bootstrap"
    / "templates"
    / "release_gate.py"
)

_BUMP = "bump-version-main"
_BOT = "github-actions[bot]"


@pytest.fixture(scope="module")
def gate() -> types.ModuleType:
    """Load the vendored gate script as a module."""
    spec = importlib.util.spec_from_file_location("release_gate", _TEMPLATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    state: str | None,
    *,
    head_ref: str = _BUMP,
    labels: list[str] | None = None,
    creator: str = _BOT,
    raw: str | None = None,
) -> int:
    """Run ``main`` with a state file holding one ``state`` status posted by
    ``creator`` (None: no file; ``""``: an empty file). ``raw`` is written
    verbatim instead."""
    state_file = tmp_path / "gate-check-state.txt"
    if raw is not None:
        state_file.write_text(raw)
    elif state is not None:
        state_file.write_text(f"{state} {creator}\n" if state else "")
    return gate.main(
        [
            "--head-ref",
            head_ref,
            "--labels",
            json.dumps(labels if labels is not None else ["release"]),
            "--gate-check-state-file",
            str(state_file),
        ],
    )


@pytest.mark.parametrize("state", [None, "", "success", "failure", "pending"])
def test_non_release_pr_always_passes(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    state: str | None,
) -> None:
    """The gate exists only for bump PRs; every other PR passes untouched."""
    assert _run(gate, tmp_path, state, head_ref="feat/x", labels=["size/S"]) == 0


def test_bump_branch_is_a_release_pr_without_the_label(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    """The `release` label is added after the PR opens, so the `opened` payload
    can lack it. The branch alone must make the PR a release PR, or the first
    run on every bump PR would pass vacuously."""
    assert _run(gate, tmp_path, "", labels=[]) == 1


def test_release_label_alone_is_a_release_pr(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    assert _run(gate, tmp_path, "", head_ref="hotfix", labels=["release"]) == 1


def test_passed_release_gate_passes(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    assert _run(gate, tmp_path, "success") == 0


@pytest.mark.parametrize("labels", [["release", "gate-check"], ["release", "e2e"]])
def test_a_label_is_not_a_verdict(
    gate: types.ModuleType, tmp_path: pathlib.Path, labels: list[str]
) -> None:
    """The old opt-in labels no longer pass the gate (FND-3839): a label says a
    run was asked for, not that it ran and passed."""
    assert _run(gate, tmp_path, "", labels=labels) == 1


def test_missing_hint_names_the_command(
    gate: types.ModuleType, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(gate, tmp_path, "") == 1
    assert "/gate-check" in capsys.readouterr().out


def test_pending_says_it_is_running(
    gate: types.ModuleType, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(gate, tmp_path, "pending") == 1
    assert "still running" in capsys.readouterr().out


@pytest.mark.parametrize("state", [None, ""])
def test_no_verdict_fails_closed(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    state: str | None,
) -> None:
    """A new head has no status, so a push re-blocks until `/gate-check`.

    An absent file and an empty one are the failed-lookup signal, and the
    gate is fail-closed on it.
    """
    assert _run(gate, tmp_path, state) == 1


@pytest.mark.parametrize("state", ["failure", "error"])
def test_failed_release_gate_fails(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    assert _run(gate, tmp_path, state) == 1
    assert "failed" in capsys.readouterr().out


def test_the_newest_state_line_wins(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
) -> None:
    """The statuses list is newest first, so the first bot line is the verdict.
    Blank lines from `--jq` do not hide it."""
    raw = f"\nsuccess {_BOT}\nfailure {_BOT}\npending {_BOT}\n\n"
    assert _run(gate, tmp_path, None, raw=raw) == 0
    raw = f"failure {_BOT}\nsuccess {_BOT}\n"
    assert _run(gate, tmp_path, None, raw=raw) == 1


@pytest.mark.parametrize(
    "creator", ["a-maintainer", "renovate[bot]", "github-actions", "atlan-ci"]
)
def test_a_verdict_from_anyone_else_is_not_a_verdict(
    gate: types.ModuleType, tmp_path: pathlib.Path, creator: str
) -> None:
    """FND-3947: on a bump PR this gate is the only blocker, because the gated
    reusables skip their jobs there. Anyone with write access can post a
    `gate-check: success` through the API, so only the identity
    test-commands posts with may pass it."""
    assert _run(gate, tmp_path, "success", creator=creator) == 1


def test_a_forged_newer_success_does_not_mask_the_real_verdict(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    raw = f"success a-maintainer\nfailure {_BOT}\n"
    assert _run(gate, tmp_path, None, raw=raw) == 1
    raw = f"failure a-maintainer\nsuccess {_BOT}\n"
    assert _run(gate, tmp_path, None, raw=raw) == 0


@pytest.mark.parametrize("raw", ["success\n", f"success {_BOT} extra\n"])
def test_a_line_in_any_other_shape_is_ignored(
    gate: types.ModuleType, tmp_path: pathlib.Path, raw: str
) -> None:
    """A bare state (the pre-FND-3947 combined-status format) names no creator,
    so it cannot pass."""
    assert _run(gate, tmp_path, None, raw=raw) == 1


def test_unparseable_labels_read_as_none(gate: types.ModuleType) -> None:
    """Garbage in the labels argument cannot be mistaken for `release`."""
    assert gate.parse_labels("not json") == []
    assert gate.parse_labels('{"release": true}') == []
    assert gate.parse_labels('["release", 3]') == ["release"]


def test_output_is_ascii_only() -> None:
    """Vendored into every repo; a non-ASCII byte trips some fleet linters."""
    _TEMPLATE.read_text(encoding="utf-8").encode("ascii")


def test_script_is_vendored_where_the_workflow_calls_it() -> None:
    """The workflow runs `.github/scripts/release_gate.py`; bootstrap writes it."""
    assert (".github/scripts/release_gate.py", "release_gate.py") in (
        MANAGED_ACTION_FILES
    )
    steps = yaml.safe_load(render("release-gate.yaml"))["jobs"]["release-gate"]["steps"]
    verify = next(s for s in steps if s.get("name") == "Verify release readiness")
    assert "python3 .github/scripts/release_gate.py" in verify["run"]
    assert '--head-ref "$HEAD_REF"' in verify["run"]
    assert verify["env"]["HEAD_REF"] == "${{ github.head_ref }}"


def _fetch_step() -> dict:
    steps = yaml.safe_load(render("release-gate.yaml"))["jobs"]["release-gate"]["steps"]
    return next(
        s for s in steps if s.get("name") == "Fetch the head commit's gate-check status"
    )


def test_status_lookup_reads_the_head_commit_and_fails_soft() -> None:
    """The status lives on the PR head, not the merge ref the run checks out.

    The lookup must be `continue-on-error`: a failure leaves the file empty
    and the script fails the gate with a readable message, instead of the
    step failing with a bare API error. And without `statuses: read` the
    lookup 403s on every run and no release could ever pass.
    """
    workflow = yaml.safe_load(render("release-gate.yaml"))
    assert workflow["permissions"]["statuses"] == "read"
    fetch = _fetch_step()
    assert fetch["continue-on-error"] is True
    assert fetch["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'select(.context == "gate-check")' in fetch["run"]


def test_status_lookup_reads_each_status_creator() -> None:
    """Only the per-commit statuses list carries `creator`; the combined
    `/status` endpoint does not, so the creator check would see nothing."""
    run = _fetch_step()["run"]
    assert "/commits/${HEAD_SHA}/statuses?" in run
    assert '"\\(.state) \\(.creator.login)"' in run


def test_the_trusted_creator_is_the_token_test_commands_posts_with() -> None:
    """`github.token` posts as github-actions[bot]. If test-commands ever
    switches to an App token, every release PR blocks here until this moves."""
    reusable = (
        pathlib.Path(__file__).resolve().parents[3]
        / ".github/workflows/test-commands-reusable.yaml"
    )
    steps = [
        step
        for job in yaml.safe_load(reusable.read_text())["jobs"].values()
        for step in job.get("steps", [])
        if "gate_check_verdict.py" in step.get("run", "")
    ]
    assert steps and all(s["env"]["GH_TOKEN"] == "${{ github.token }}" for s in steps)


def test_status_lookup_walks_every_page() -> None:
    """`gate-check` can sit past the first page on a commit with many contexts.

    And `--slurp` must never join `--jq`: gh rejects the pair outright, which
    would leave the state file empty - a release PR blocked on every run.
    """
    fetch = _fetch_step()
    assert "--paginate" in fetch["run"]
    assert "per_page=100" in fetch["run"]
    assert "--slurp" not in fetch["run"]


def test_gate_does_not_subscribe_to_label_events() -> None:
    """FND-3839: a label event on the same commit only ever added a duplicate
    run. The verdict landing re-runs this workflow instead."""
    types_ = yaml.safe_load(render("release-gate.yaml"))[True]["pull_request"]["types"]
    assert "labeled" not in types_
    assert "unlabeled" not in types_


def test_status_context_matches_the_command_that_writes_it() -> None:
    """The gate reads the context pr_test_command.py and gate_check_verdict.py
    write; a rename on either side would block every release PR."""
    import re

    script = (
        pathlib.Path(__file__).resolve().parents[3]
        / ".github/scripts/pr_test_command.py"
    )
    match = re.search(r'^GATE_CHECK_CONTEXT = "([^"]+)"$', script.read_text(), re.M)
    assert match is not None
    assert f'select(.context == "{match.group(1)}")' in _fetch_step()["run"]


# -- Unreleased SDK pin ------------------------------------------------------

_REGISTRY = '{ registry = "https://pypi.org/simple" }'
_GIT = '{ git = "https://github.com/example/sdk?rev=abc#abc" }'


def _lock(name: str, version: str, source: str) -> str:
    return (
        "version = 1\n\n"
        '[[package]]\nname = "example-app"\nversion = "1.0.0"\n'
        'source = { virtual = "." }\n\n'
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
        f"source = {source}\n"
    )


def _pins(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    lock: str | None,
    pyproject: str | None = None,
) -> list[str]:
    lock_file = tmp_path / "uv.lock"
    project_file = tmp_path / "pyproject.toml"
    if lock is not None:
        lock_file.write_text(lock)
    if pyproject is not None:
        project_file.write_text(pyproject)
    return gate.unreleased_sdk_pins(str(lock_file), str(project_file))


def test_a_released_sdk_from_an_index_is_not_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    assert _pins(gate, tmp_path, lock) == []


@pytest.mark.parametrize(
    "source",
    [_GIT, '{ path = "../application-sdk" }', '{ editable = "../sdk" }'],
)
def test_an_sdk_locked_off_an_index_is_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path, source: str
) -> None:
    found = _pins(gate, tmp_path, _lock("atlan-application-sdk", "3.57.0", source))
    assert len(found) == 1
    assert "atlan-application-sdk is locked from a" in found[0]


@pytest.mark.parametrize(
    "version", ["3.58.0rc1", "0.1.1rc62", "4.0.0a2", "3.58.0.dev4"]
)
def test_a_prerelease_sdk_is_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path, version: str
) -> None:
    found = _pins(gate, tmp_path, _lock("atlan-application-sdk", version, _REGISTRY))
    assert found == [f"atlan-application-sdk is locked to pre-release {version}"]


def test_a_post_release_is_released(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    lock = _lock("atlan-application-sdk", "3.57.0.post1", _REGISTRY)
    assert _pins(gate, tmp_path, lock) == []


@pytest.mark.parametrize(
    "name", ["atlan-application-sdk-conformance", "atlan-application-sdk-api"]
)
def test_every_sdk_distribution_is_covered(
    gate: types.ModuleType, tmp_path: pathlib.Path, name: str
) -> None:
    assert len(_pins(gate, tmp_path, _lock(name, "1.0.0", _GIT))) == 1


def test_another_package_from_git_is_not_this_gates_business(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    """Scoped to the SDK: an app may ship a git-tagged helper library."""
    assert _pins(gate, tmp_path, _lock("query-redaction", "0.1.0rc1", _GIT)) == []


def test_a_uv_sources_redirect_is_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    pyproject = (
        '[project]\nname = "example-app"\n\n[tool.uv.sources]\n'
        'atlan-application-sdk = { git = "https://github.com/example/sdk" }\n'
    )
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    found = _pins(gate, tmp_path, lock, pyproject)
    assert found == ["atlan-application-sdk is redirected by [tool.uv.sources]"]


def test_a_named_index_or_a_commented_source_is_not_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    pyproject = (
        '[project]\nname = "example-app"\n\n[tool.uv.sources]\n'
        'atlan-application-sdk = { index = "internal" }\n'
        '# atlan-application-sdk-api = { path = "../application-sdk" }\n'
    )
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    assert _pins(gate, tmp_path, lock, pyproject) == []


def test_a_marker_specific_array_of_named_indexes_is_not_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    pyproject = (
        '[project]\nname = "example-app"\n\n[tool.uv.sources]\n'
        "atlan-application-sdk = [\n"
        '  { index = "internal", marker = "sys_platform == \'linux\'" },\n'
        '  { index = "pypi", marker = "sys_platform != \'linux\'" },\n'
        "]\n"
    )
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    assert _pins(gate, tmp_path, lock, pyproject) == []


def test_a_marker_specific_array_with_a_redirect_is_a_pin(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    pyproject = (
        '[project]\nname = "example-app"\n\n[tool.uv.sources]\n'
        "atlan-application-sdk = [\n"
        '  { index = "internal", marker = "sys_platform == \'linux\'" },\n'
        '  { path = "../sdk", marker = "sys_platform != \'linux\'" },\n'
        "]\n"
    )
    lock = _lock("atlan-application-sdk", "3.57.0", _REGISTRY)
    found = _pins(gate, tmp_path, lock, pyproject)
    assert found == ["atlan-application-sdk is redirected by [tool.uv.sources]"]


def test_the_sdk_repo_itself_is_never_a_finding(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    pyproject = '[project]\nname = "atlan-application-sdk"\n'
    lock = _lock("atlan-application-sdk", "3.58.0", '{ editable = "." }')
    assert _pins(gate, tmp_path, lock, pyproject) == []


def test_no_lock_has_nothing_to_check_but_a_broken_one_fails(
    gate: types.ModuleType, tmp_path: pathlib.Path
) -> None:
    assert _pins(gate, tmp_path, None) == []
    assert _pins(gate, tmp_path, "this is [not toml") == [gate.LOCK_UNREADABLE]


def test_a_pin_fails_a_release_pr_even_with_a_passed_gate(
    gate: types.ModuleType,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The verdict says the tests passed, not that the SDK under them shipped."""
    lock_file = tmp_path / "uv.lock"
    lock_file.write_text(_lock("atlan-application-sdk", "3.57.0", _GIT))
    state_file = tmp_path / "gate-check-state.txt"
    state_file.write_text(f"success {_BOT}\n")
    argv = ["--gate-check-state-file", str(state_file), "--lock-file", str(lock_file)]

    assert gate.main(["--head-ref", _BUMP, *argv]) == 1
    out = capsys.readouterr().out
    assert "Unreleased SDK pin" in out and "/gate-check" in out

    assert gate.main(["--head-ref", "feat/try-sdk-branch", *argv]) == 0


def test_the_workflow_hands_the_gate_both_files() -> None:
    steps = yaml.safe_load(render("release-gate.yaml"))["jobs"]["release-gate"]["steps"]
    verify = next(s for s in steps if s.get("name") == "Verify release readiness")
    assert "--lock-file uv.lock" in verify["run"]
    assert "--pyproject pyproject.toml" in verify["run"]
