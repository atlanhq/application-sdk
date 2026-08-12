"""Tests for .github/scripts/detect_merge_queue.py.

`gh` is stubbed through the module's single `run` seam (per the testability-seam
convention in docs/standards/ci.md), so every branch — queue present, queue
absent, per-branch scoping, and each fail-open path — is exercised without
network access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from detect_merge_queue import detect, governs_branch, main, ref_matches  # noqa: E402

REPO = "atlanhq/atlan-example-app"


def _ruleset(
    *,
    rule_types=("merge_queue",),
    enforcement="active",
    target="branch",
    include=("~DEFAULT_BRANCH",),
    exclude=(),
) -> dict:
    return {
        "id": 1,
        "name": "main",
        "target": target,
        "enforcement": enforcement,
        "conditions": {
            "ref_name": {"include": list(include), "exclude": list(exclude)}
        },
        "rules": [{"type": t} for t in rule_types],
    }


def _stub(listing, detail):
    """Build a `run` double returning `listing` then `detail` per ruleset id."""

    def run(args: list) -> str:
        path = args[1]
        if path.endswith("/rulesets"):
            return json.dumps(listing)
        return json.dumps(detail)

    return run


# --- ref_name matching -----------------------------------------------------


@pytest.mark.parametrize(
    "patterns,base_ref,expected",
    [
        (["~ALL"], "anything", True),
        (["~DEFAULT_BRANCH"], "main", True),
        (["~DEFAULT_BRANCH"], "release/1.x", False),
        (["refs/heads/main"], "main", True),
        (["refs/heads/main"], "refs/heads/main", True),
        (["refs/heads/release/*"], "release/1.x", True),
        (["refs/heads/release/*"], "main", False),
        ([], "main", False),
        ([None], "main", False),  # malformed entries are ignored, not crashes
    ],
)
def test_ref_matches(patterns, base_ref, expected) -> None:
    assert ref_matches(patterns, base_ref, "main") is expected


# --- governs_branch --------------------------------------------------------


def test_active_merge_queue_on_default_branch_governs() -> None:
    assert governs_branch(_ruleset(), "main", "main") is True


def test_ruleset_without_merge_queue_rule_does_not_govern() -> None:
    # The real shape of a consumer that has a ruleset but no queue (e.g. a
    # "block-pr" ruleset): rules exist, none of them is merge_queue.
    ruleset = _ruleset(rule_types=("pull_request", "required_status_checks"))
    assert governs_branch(ruleset, "main", "main") is False


@pytest.mark.parametrize("enforcement", ["evaluate", "disabled"])
def test_non_active_enforcement_does_not_govern(enforcement) -> None:
    # "evaluate" is GitHub's dry-run mode — it reports but never queues a merge.
    assert governs_branch(_ruleset(enforcement=enforcement), "main", "main") is False


def test_non_branch_target_does_not_govern() -> None:
    assert governs_branch(_ruleset(target="tag"), "main", "main") is False


def test_queue_is_scoped_per_branch_not_per_repo() -> None:
    # A repo can queue main while leaving a release branch unqueued.
    ruleset = _ruleset(include=("refs/heads/main",))
    assert governs_branch(ruleset, "main", "main") is True
    assert governs_branch(ruleset, "release/1.x", "main") is False


def test_exclude_overrides_include() -> None:
    ruleset = _ruleset(include=("~ALL",), exclude=("refs/heads/release/*",))
    assert governs_branch(ruleset, "main", "main") is True
    assert governs_branch(ruleset, "release/1.x", "main") is False


def test_malformed_ruleset_does_not_govern() -> None:
    assert governs_branch(None, "main", "main") is False
    assert governs_branch({}, "main", "main") is False


# --- detect (end to end over the stubbed seam) -----------------------------


def test_detect_true_when_queue_present() -> None:
    run = _stub([{"id": 1}], _ruleset())
    assert detect(REPO, "main", "main", run=run) is True


def test_detect_false_when_repo_has_no_rulesets() -> None:
    # The common fleet state: zero rulesets ⇒ no queue ⇒ PR tier runs.
    run = _stub([], {})
    assert detect(REPO, "main", "main", run=run) is False


def test_detect_false_when_ruleset_lacks_queue() -> None:
    run = _stub([{"id": 1}], _ruleset(rule_types=("pull_request",)))
    assert detect(REPO, "main", "main", run=run) is False


# --- fail-open paths -------------------------------------------------------
# Every failure must yield False (⇒ integration runs on the PR). Failing the
# other way would silently restore the ungated-integration gap.


def test_detect_fails_open_on_api_error() -> None:
    assert detect(REPO, "main", "main", run=lambda _args: "") is False


def test_detect_fails_open_on_invalid_json() -> None:
    assert detect(REPO, "main", "main", run=lambda _args: "not json") is False


def test_detect_fails_open_on_unexpected_payload_shape() -> None:
    # A 403 body is a JSON *object*, not the expected list of rulesets.
    run = _stub({"message": "Resource not accessible by integration"}, {})
    assert detect(REPO, "main", "main", run=run) is False


def test_detect_fails_open_when_detail_fetch_fails() -> None:
    def run(args: list) -> str:
        return json.dumps([{"id": 1}]) if args[1].endswith("/rulesets") else ""

    assert detect(REPO, "main", "main", run=run) is False


def test_detect_skips_entries_without_id() -> None:
    run = _stub([{"name": "no-id"}], _ruleset())
    assert detect(REPO, "main", "main", run=run) is False


def test_detect_flattens_slurped_pages() -> None:
    # `--paginate --slurp` wraps each page in its own array; without flattening,
    # a repo with more than one page of rulesets would parse as "no queue".
    run = _stub([[{"id": 1}], [{"id": 2}]], _ruleset())
    assert detect(REPO, "main", "main", run=run) is True


def test_detect_passes_slurp_to_gh() -> None:
    seen: list[list] = []

    def run(args: list) -> str:
        seen.append(args)
        return json.dumps([])

    detect(REPO, "main", "main", run=run)
    assert seen and "--slurp" in seen[0] and "--paginate" in seen[0]


# --- CLI output ------------------------------------------------------------


@pytest.mark.parametrize(
    "listing,detail,expected",
    [([{"id": 1}], _ruleset(), "enabled=true"), ([], {}, "enabled=false")],
)
def test_main_emits_github_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    listing,
    detail,
    expected,
) -> None:
    monkeypatch.setattr("detect_merge_queue._run_gh", _stub(listing, detail))
    rc = main(["--repo", REPO, "--base-ref", "main", "--default-branch", "main"])
    assert rc == 0
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    "listing,detail,expected",
    [([{"id": 1}], _ruleset(), "enabled=true\n"), ([], {}, "enabled=false\n")],
)
def test_stdout_is_exactly_the_github_output_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    listing,
    detail,
    expected,
) -> None:
    """stdout must be `enabled=<bool>` and NOTHING else.

    The caller redirects this script's stdout straight into $GITHUB_OUTPUT, and
    the runner rejects any line without an `=` — which fails the step and, via
    the gate's detect-merge-queue rule, reddens Tests Gate on every pull_request
    in every consumer. A substring assertion (the test above) cannot catch a
    stray line, so this one pins the whole stream by equality.
    """
    monkeypatch.setattr("detect_merge_queue._run_gh", _stub(listing, detail))
    assert main(["--repo", REPO, "--base-ref", "main", "--default-branch", "main"]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected
    # The human-readable annotation still has to be emitted — on stderr.
    assert "::notice::" in captured.err


def test_no_stdout_line_lacks_an_equals_sign(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The precise property the runner enforces on $GITHUB_OUTPUT, stated directly
    # so a future line added to stdout fails here rather than in the fleet.
    monkeypatch.setattr("detect_merge_queue._run_gh", lambda _args: "")
    main(["--repo", REPO, "--base-ref", "main"])
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if "=" not in line] == []


def test_unreadable_rulesets_warns_rather_than_failing_silently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A 403 body returns "no queue" like a genuinely queue-less repo does; the
    # warning is the only thing distinguishing them in the log.
    monkeypatch.setattr(
        "detect_merge_queue._run_gh",
        lambda _args: '{"message": "Resource not accessible by integration"}',
    )
    main(["--repo", REPO, "--base-ref", "main"])
    captured = capsys.readouterr()
    assert "enabled=false\n" == captured.out
    assert "could not read rulesets" in captured.err


def test_main_never_exits_nonzero_on_api_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A detection outage must not fail the job — it degrades to the PR tier.
    monkeypatch.setattr("detect_merge_queue._run_gh", lambda _args: "")
    assert main(["--repo", REPO, "--base-ref", "main"]) == 0
    assert "enabled=false" in capsys.readouterr().out
