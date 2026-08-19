"""Tests for conformance_remediate_request.py."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import urllib.error

import pytest

_MOD_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "conformance_remediate_request.py"
)
_spec = importlib.util.spec_from_file_location(
    "conformance_remediate_request", _MOD_PATH
)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ── command parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/remediate", []),
        ("/remediate\n", []),
        ("  /remediate  ", []),
        ("/remediate L004", ["L004"]),
        ("/remediate l004", ["L004"]),
        ("/remediate L004,E002", ["L004", "E002"]),
        ("/remediate L004 E002", ["L004", "E002"]),
        ("/remediate L004, E002\nplease", ["L004", "E002"]),
        ("/REMEDIATE L004", ["L004"]),
    ],
)
def test_parse_command_extracts_rules(body: str, expected: list[str]) -> None:
    assert mod.parse_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "",
        "looks like we should /remediate this",
        "> /remediate L004",  # quoting an earlier comment
        "please fix the conformance findings",
        "/remediate-all",
    ],
)
def test_parse_command_rejects_non_invocations(body: str) -> None:
    """Anchored at the start, so a mention or a quote does not fire a run."""
    with pytest.raises(mod.NotACommand):
        mod.parse_command(body)


def test_slash_remediate_all_is_not_the_command() -> None:
    """`\\b` after `/remediate` must not match `/remediate-all`."""
    with pytest.raises(mod.NotACommand):
        mod.parse_command("/remediate-all")


def test_rule_ids_only_from_the_command_line_not_the_whole_comment() -> None:
    """A rule mentioned in prose below the command must not be picked up."""
    assert mod.parse_command("/remediate L004\nwhile you're there, E002 too") == [
        "L004"
    ]


# ── request shape ─────────────────────────────────────────────────────────


def _req(**over: object) -> dict:
    base = dict(
        repo="atlanhq/atlan-netsuite-app",
        pr_number=123,
        head_ref="feature/x",
        head_sha="abc123",
        rules=["L004"],
        commenter="someone",
        gha_run_url="https://gha/run/1",
        suite_version="0.20.1",
    )
    base.update(over)
    return mod.build_request(**base)  # type: ignore[arg-type]


def test_pr_surface_pushes_to_the_pr_branch_not_a_new_pr() -> None:
    """The author already has a PR; a PR to fix a PR is the wrong shape."""
    assert _req()["policy"]["delivery"] == "push_to_pr_branch"


def test_pr_surface_uses_the_interactive_lane() -> None:
    """A human is waiting — it must not queue behind the fleet sweep."""
    assert _req()["policy"]["lane"] == "interactive"


def test_head_sha_is_pinned_into_the_scope() -> None:
    """So the rover can refuse a branch that moved between the ask and the run."""
    assert _req(head_sha="deadbeef")["scope"]["head_sha"] == "deadbeef"


def test_empty_rules_means_every_failing_rule() -> None:
    assert _req(rules=[])["scope"]["rules"] == []


def test_both_tiers_are_in_scope_for_the_pr_surface() -> None:
    """`failing` counts BLOCK only, so a warnings-only PR must still be actionable."""
    assert _req()["scope"]["tiers"] == ["block", "warn"]


def test_suite_version_is_carried_and_pinned() -> None:
    assert _req(suite_version="0.19.1")["policy"]["suite_version"] == "0.19.1"


def test_default_suite_version_is_pinned_not_latest() -> None:
    assert mod.DEFAULT_SUITE_VERSION != "latest"
    assert mod.DEFAULT_SUITE_VERSION[0].isdigit()


def test_idempotency_key_is_keyed_on_the_head_sha() -> None:
    """Re-asking on the same commit dedupes; a new commit is a new request."""
    a = _req(head_sha="sha1")["idempotency_key"]
    b = _req(head_sha="sha2")["idempotency_key"]
    assert a != b
    assert "sha1" in a


def test_idempotency_key_distinguishes_rule_sets() -> None:
    assert (
        _req(rules=["L004"])["idempotency_key"]
        != _req(rules=["E002"])["idempotency_key"]
    )
    assert _req(rules=[])["idempotency_key"].endswith(":all")


def test_report_sinks_are_the_github_ones() -> None:
    assert _req()["report_to"] == ["github_check", "github_comment"]


def test_origin_identifies_the_surface() -> None:
    r = _req()
    assert r["origin"] == "github-pr-comment"
    assert r["origin_id"] == "atlanhq/atlan-netsuite-app#123"


def test_request_is_json_serialisable() -> None:
    json.dumps(_req())


# ── PR context ────────────────────────────────────────────────────────────


def test_pr_context_parses_gh_output() -> None:
    def fake(args: list[str]) -> str:
        return json.dumps(
            {
                "headRefName": "feature/x",
                "headRefOid": "abc123",
                "isDraft": False,
                "state": "OPEN",
            }
        )

    ctx = mod.pr_context("o/r", "1", runner=fake)
    assert ctx == {
        "head_ref": "feature/x",
        "head_sha": "abc123",
        "is_draft": False,
        "state": "OPEN",
    }


def test_pr_context_tolerates_nulls() -> None:
    def fake(args: list[str]) -> str:
        return json.dumps({"headRefName": None, "headRefOid": None, "state": None})

    ctx = mod.pr_context("o/r", "1", runner=fake)
    assert ctx["head_ref"] == ""
    assert ctx["head_sha"] == ""


# ── exit decisions ────────────────────────────────────────────────────────


def test_202_is_success_and_reports_the_run_id() -> None:
    code, msg = mod.decide_exit(202, json.dumps({"run_id": "run-7"}))
    assert code == 0
    assert "run-7" in msg


def test_200_is_also_accepted() -> None:
    assert mod.decide_exit(200, "{}")[0] == 0


def test_unparseable_success_body_still_succeeds() -> None:
    """The dispatch worked; a body we cannot parse is not a reason to fail red."""
    code, _ = mod.decide_exit(202, "not json")
    assert code == 0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500, 503])
def test_non_success_statuses_fail_with_an_annotation(status: int) -> None:
    code, msg = mod.decide_exit(status, '{"detail":"nope"}')
    assert code == 1
    assert msg.startswith("::error::")
    assert str(status) in msg


def test_failure_message_truncates_a_huge_body() -> None:
    code, msg = mod.decide_exit(500, "x" * 10_000)
    assert code == 1
    assert len(msg) < 600


# ── health check ──────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def test_health_succeeds_on_first_200() -> None:
    calls: list[str] = []

    def opener(url: str, timeout: int = 0) -> _Resp:
        calls.append(url)
        return _Resp(200)

    assert mod.check_health("https://m", opener=opener, sleeper=lambda _: None)
    assert calls == ["https://m/health"]


def test_health_retries_then_gives_up() -> None:
    slept: list[float] = []

    def opener(url: str, timeout: int = 0) -> _Resp:
        raise urllib.error.URLError("down")

    assert not mod.check_health("https://m", opener=opener, sleeper=slept.append)
    # Sleeps between attempts, not after the last one.
    assert len(slept) == mod.HEALTH_RETRIES - 1


def test_health_treats_non_200_as_a_retry() -> None:
    def opener(url: str, timeout: int = 0) -> _Resp:
        return _Resp(503)

    assert not mod.check_health("https://m", opener=opener, sleeper=lambda _: None)


# ── main ──────────────────────────────────────────────────────────────────


def test_main_exits_zero_when_the_comment_is_not_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "nice work!")
    assert mod.main() == 0


def test_main_dry_run_prints_the_payload_and_skips_the_post(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "/remediate L004")
    monkeypatch.setenv("REPO", "atlanhq/atlan-netsuite-app")
    monkeypatch.setenv("PR_NUMBER", "123")
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(
        mod,
        "pr_context",
        lambda r, p: {
            "head_ref": "feature/x",
            "head_sha": "abc123",
            "is_draft": False,
            "state": "OPEN",
        },
    )
    assert mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"]["rules"] == ["L004"]
    assert payload["policy"]["delivery"] == "push_to_pr_branch"


def test_main_refuses_a_closed_pr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "/remediate")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setattr(
        mod,
        "pr_context",
        lambda r, p: {
            "head_ref": "x",
            "head_sha": "s",
            "is_draft": False,
            "state": "MERGED",
        },
    )
    assert mod.main() == 1
    assert "MERGED" in capsys.readouterr().out


def test_main_refuses_when_the_head_sha_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "/remediate")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setattr(
        mod,
        "pr_context",
        lambda r, p: {
            "head_ref": "x",
            "head_sha": "",
            "is_draft": False,
            "state": "OPEN",
        },
    )
    assert mod.main() == 1
    assert "head SHA" in capsys.readouterr().out


def test_main_rejects_a_non_integer_pr_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "/remediate")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "not-a-number")
    assert mod.main() == 1
    assert "not an integer" in capsys.readouterr().out


def test_main_surfaces_a_gh_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "/remediate")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")

    def boom(repo: str, pr: str) -> dict:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="no such PR")

    monkeypatch.setattr(mod, "pr_context", boom)
    assert mod.main() == 1
    assert "could not read PR" in capsys.readouterr().out


def test_main_requires_url_and_token_for_a_real_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMMENT_BODY", "/remediate")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("MOTHERSHIP_URL", "")
    monkeypatch.setenv("HARNESS_TOKEN", "")
    monkeypatch.setattr(
        mod,
        "pr_context",
        lambda r, p: {
            "head_ref": "x",
            "head_sha": "s",
            "is_draft": False,
            "state": "OPEN",
        },
    )
    assert mod.main() == 1
    assert "MOTHERSHIP_URL and HARNESS_TOKEN" in capsys.readouterr().out
