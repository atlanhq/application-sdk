"""Tests for the connector-review gate bootstrap vendors into consumer repos.

The gate decides whether a PR has been reviewed by the connector review
lane, by matching the trailer mothership appends to every connector
review body. The load-bearing property is that the trailer only counts at
the TAIL of the body — otherwise a PR author could paste the marker into
their own description and self-certify.

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
from conformance.bootstrap.render import render

_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "conformance"
    / "bootstrap"
    / "templates"
    / "connector_review_gate.py"
)

_BOT = "mothership-reviewer[bot]"
_SHA = "b304db1b7f6d59715dea2f703b8e10ee7766cdb2"

# The real trailer, byte-for-byte off atlan-databricks-app#109.
_TRAILER = f"<!-- commit:{_SHA} mode:standard -->\n<!-- profile:connector-app -->"


@pytest.fixture(scope="module")
def gate() -> types.ModuleType:
    """Load the vendored gate script as a module."""
    spec = importlib.util.spec_from_file_location("connector_review_gate", _TEMPLATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _review(body: str, login: str = _BOT) -> dict:
    return {"user": {"login": login}, "body": body}


def test_accepts_the_real_trailer(gate: types.ModuleType) -> None:
    """A genuine connector review is found."""
    assert gate.find_review([_review(f"Looks good.\n\n{_TRAILER}")]) == _SHA


def test_rejects_a_marker_that_is_not_at_the_tail(gate: types.ModuleType) -> None:
    """A quoted marker mid-body must not count — this is the spoof guard."""
    body = f"Here is what a trailer looks like:\n\n{_TRAILER}\n\nAnyway, LGTM."
    assert gate.find_review([_review(body)]) is None


def test_rejects_a_non_reviewer_author(gate: types.ModuleType) -> None:
    """A human pasting the trailer does not satisfy the gate."""
    assert gate.find_review([_review(_TRAILER, login="some-human")]) is None


def test_rejects_the_security_lane(gate: types.ModuleType) -> None:
    """A security review carries mode:security and no connector profile."""
    body = f"<!-- commit:{_SHA} mode:security -->"
    assert gate.find_review([_review(body)]) is None


def test_accepts_the_legacy_footer_form(gate: types.ModuleType) -> None:
    """Reviews predating the machine marker carry a rendered footer."""
    body = f"Findings.\n\nprofile: connector-app\n<!-- commit:{_SHA} mode:standard -->"
    assert gate.find_review([_review(body)]) == _SHA


def test_strict_mode_requires_the_current_head(gate: types.ModuleType) -> None:
    """With head_sha set, a review of an older commit does not count."""
    reviews = [_review(_TRAILER)]
    assert gate.find_review(reviews, head_sha=_SHA) == _SHA
    assert gate.find_review(reviews, head_sha="a" * 40) is None


def test_flattens_slurped_pages(gate: types.ModuleType) -> None:
    """`gh api --paginate --slurp` nests each page in an outer list."""
    page = [_review(_TRAILER)]
    assert gate.flatten_pages([page, []]) == page
    assert gate.flatten_pages(page) == page
    assert gate.flatten_pages("not a list") == []


def test_exempt_author_passes_without_a_review(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renovate must never need a review, or fleet auto-merge stops."""
    monkeypatch.setattr("sys.stdin", _stdin("[]"))
    assert gate.main(["--author", "renovate[bot]", "--enforce"]) == 0


def test_missing_review_fails_only_when_enforced(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dormant by default so the gate can ship before it is armed."""
    monkeypatch.setattr("sys.stdin", _stdin("[]"))
    assert gate.main([]) == 0
    monkeypatch.setattr("sys.stdin", _stdin("[]"))
    assert gate.main(["--enforce"]) == 1


def test_present_review_passes_when_enforced(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path under enforcement."""
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps([[_review(_TRAILER)]])))
    assert gate.main(["--enforce"]) == 0


def test_repo_without_reviewer_config_passes(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A repo not on the connector lane is not gated by this at all."""
    monkeypatch.setattr("sys.stdin", _stdin("[]"))
    absent = str(tmp_path / "reviewer.yaml")
    assert gate.main(["--enforce", "--reviewer-config", absent]) == 0


def test_policy_head_requires_the_current_commit(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--policy head` is the opt-in that closes the sticky gap.

    Both `--policy` and `--head-sha` are always passed by the workflow, so
    the default must ignore head_sha rather than requiring the caller to
    omit it -- that is what keeps the caller's shell free of branching.
    """
    payload = json.dumps([[_review(_TRAILER)]])
    stale = ["--enforce", "--head-sha", "c" * 40]

    monkeypatch.setattr("sys.stdin", _stdin(payload))
    assert gate.main([*stale, "--policy", "sticky"]) == 0

    monkeypatch.setattr("sys.stdin", _stdin(payload))
    assert gate.main([*stale, "--policy", "head"]) == 1

    monkeypatch.setattr("sys.stdin", _stdin(payload))
    assert gate.main(["--enforce", "--policy", "head", "--head-sha", _SHA]) == 0


def test_default_policy_is_sticky(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting --policy must behave as sticky, not as head."""
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps([[_review(_TRAILER)]])))
    assert gate.main(["--enforce", "--head-sha", "c" * 40]) == 0


def test_check_run_name_is_the_ruleset_contract() -> None:
    """The job name IS the required-status-check context. Renaming un-protects.

    A ruleset matches on the check-run name, which for an inline job is just
    the job's `name:`. If this drifts, every repo's ruleset requires a context
    no check ever reports, and the PRs sit at "Expected" forever instead of
    failing loudly. Pin it.
    """
    workflow = yaml.safe_load(render("connector-review-gate.yaml"))
    jobs = workflow["jobs"]
    assert [job["name"] for job in jobs.values()] == ["Connector Review"]


def test_gate_reruns_when_the_review_lands() -> None:
    """Without `pull_request_review`, the check stays red until a push.

    Mothership deletes the label right after dispatch and the review arrives
    minutes later, so no other PR event marks that moment. `merge_group` is
    equally load-bearing: a required context that never reports on the queue
    branch stops the queue permanently.
    """
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = yaml.safe_load(render("connector-review-gate.yaml"))[True]
    assert set(triggers) == {"pull_request", "pull_request_review", "merge_group"}
    assert triggers["pull_request_review"]["types"] == ["submitted"]


def test_output_is_ascii_only(gate: types.ModuleType) -> None:
    """Printed strings stay ASCII; log encoding is not ours to assume."""
    source = _TEMPLATE.read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if "print(" in line or line.strip().startswith(('f"', '"'))
        if any(ord(char) > 127 for char in line)
    ]
    assert not offenders, offenders


def test_empty_stdin_fails_open(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient API fault must not freeze merges fleet-wide.

    The shim fetches in a separate `continue-on-error` step whose redirect
    truncates before `gh` runs, so a GitHub 5xx or rate limit reaches this
    script as an EMPTY stdin — not as an error body. That is the shape the
    fail-open has to handle, and it must stay distinct from `[]`, which
    means "read fine, this PR has no reviews".
    """
    monkeypatch.setattr("sys.stdin", _stdin(""))
    assert gate.main(["--enforce"]) == 0
    monkeypatch.setattr("sys.stdin", _stdin("   \n"))
    assert gate.main(["--enforce"]) == 0
    # The contrast that makes the above meaningful.
    monkeypatch.setattr("sys.stdin", _stdin("[]"))
    assert gate.main(["--enforce"]) == 1


def test_unparseable_payload_fails_open(
    gate: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces: a non-JSON body also passes rather than blocks."""
    monkeypatch.setattr("sys.stdin", _stdin("<html>502</html>"))
    assert gate.main(["--enforce"]) == 0


class _stdin:  # noqa: D101
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:  # noqa: D102
        return self._text
