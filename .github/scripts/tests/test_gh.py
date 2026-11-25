"""Tests for .github/scripts/_gh.py: the shared gh CLI wrapper."""

from __future__ import annotations

import json
import subprocess
import time

import _gh
import pytest


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


class _Script:
    """A fake ``_gh.run`` that answers each call from a list, recording calls."""

    def __init__(self, *answers: subprocess.CompletedProcess[str] | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _install(monkeypatch: pytest.MonkeyPatch, *answers) -> _Script:
    script = _Script(*answers)
    monkeypatch.setattr(_gh, "run", script)
    return script


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stderr", "status"),
    [
        ("gh: Not Found (HTTP 404)", 404),
        ("gh: Server Error (HTTP 502)\n", 502),
        ("first (HTTP 500)\nthen (HTTP 403)", 403),
        ("error connecting to api.github.com", None),
        ("", None),
    ],
)
def test_parse_status(stderr: str, status: int | None) -> None:
    assert _gh.parse_status(stderr) == status


@pytest.mark.parametrize(
    ("status", "stderr", "transient"),
    [
        (500, "", True),
        (502, "Bad Gateway", True),
        (429, "", True),
        (403, "API rate limit exceeded for installation", True),
        (403, "You have exceeded a secondary rate limit", True),
        (None, "API rate limit exceeded", True),
        (None, "read tcp: connection reset by peer", True),
        (None, "net/http: TLS handshake timeout", True),
        (None, "dial tcp: lookup api.github.com: no such host", True),
        # A permission 403 or a 404 answers the same way next time.
        (403, "Resource not accessible by integration", False),
        (404, "Not Found", False),
        (422, "Validation Failed", False),
        (None, "unknown flag: --nope", False),
    ],
)
def test_is_transient(status: int | None, stderr: str, transient: bool) -> None:
    assert _gh.is_transient(status, stderr) is transient


def test_a_permission_403_is_not_a_rate_limit() -> None:
    assert not _gh.is_rate_limited(403, "Resource not accessible by integration")
    assert _gh.is_rate_limited(429, "")


# ── gh() ────────────────────────────────────────────────────────────────────


def test_gh_runs_gh_with_a_timeout_and_returns_stdout(monkeypatch) -> None:
    script = _install(monkeypatch, _ok("hello\n"))

    assert _gh.gh(["api", "user"], input="{}", timeout=7) == "hello\n"

    cmd, kwargs = script.calls[0]
    assert cmd == ["gh", "api", "user"]
    assert kwargs["timeout"] == 7
    assert kwargs["input"] == "{}"


def test_gh_has_a_default_timeout(monkeypatch) -> None:
    script = _install(monkeypatch, _ok())
    _gh.gh(["api", "user"])
    assert script.calls[0][1]["timeout"] == _gh.DEFAULT_TIMEOUT_SECONDS


def test_gh_retries_a_5xx_then_succeeds(monkeypatch) -> None:
    start = time.monotonic()
    script = _install(monkeypatch, _fail("gh: Bad Gateway (HTTP 502)"), _ok("fine"))

    assert _gh.gh(["api", "x"]) == "fine"
    assert len(script.calls) == 2
    # The conftest clock stub turns the backoff into elapsed monotonic time.
    assert time.monotonic() - start >= _gh.BACKOFF_SECONDS


def test_gh_does_not_retry_a_404(monkeypatch) -> None:
    script = _install(monkeypatch, _fail("gh: Not Found (HTTP 404)"))

    with pytest.raises(_gh.GhError) as info:
        _gh.gh(["api", "x"])

    assert info.value.status == 404
    assert "Not Found" in info.value.stderr
    assert len(script.calls) == 1


def test_gh_gives_up_after_its_retries(monkeypatch) -> None:
    script = _install(
        monkeypatch,
        _fail("(HTTP 503)"),
        _fail("(HTTP 503)"),
        _fail("(HTTP 503)"),
    )

    with pytest.raises(_gh.GhError) as info:
        _gh.gh(["api", "x"], retries=2)

    assert info.value.status == 503
    assert len(script.calls) == 3


def test_gh_retries_zero_means_one_attempt(monkeypatch) -> None:
    script = _install(monkeypatch, _fail("(HTTP 503)"))
    with pytest.raises(_gh.GhError):
        _gh.gh(["api", "x"], retries=0)
    assert len(script.calls) == 1


def test_a_timeout_is_retried_then_raised_as_timed_out(monkeypatch) -> None:
    expired = subprocess.TimeoutExpired(cmd=["gh"], timeout=5)
    script = _install(monkeypatch, expired, expired)

    with pytest.raises(_gh.GhError) as info:
        _gh.gh(["api", "x"], timeout=5, retries=1)

    assert info.value.timed_out is True
    assert info.value.status is None
    assert len(script.calls) == 2


def test_a_non_idempotent_call_is_not_repeated_after_a_5xx(monkeypatch) -> None:
    """The POST may have landed: repeating it would create a duplicate."""
    script = _install(monkeypatch, _fail("(HTTP 502)"), _ok("{}"))

    with pytest.raises(_gh.GhError):
        _gh.gh(["api", "--method", "POST", "x"], idempotent=False)

    assert len(script.calls) == 1


def test_a_non_idempotent_call_is_not_repeated_after_a_timeout(monkeypatch) -> None:
    script = _install(
        monkeypatch, subprocess.TimeoutExpired(cmd=["gh"], timeout=1), _ok("{}")
    )

    with pytest.raises(_gh.GhError) as info:
        _gh.gh(["api", "--method", "POST", "x"], idempotent=False)

    assert info.value.timed_out is True
    assert len(script.calls) == 1


def test_a_non_idempotent_call_is_retried_on_a_rate_limit(monkeypatch) -> None:
    """GitHub refuses a rate-limited request before doing the write."""
    script = _install(monkeypatch, _fail("(HTTP 429)"), _ok("{}"))

    assert _gh.gh(["api", "--method", "POST", "x"], idempotent=False) == "{}"
    assert len(script.calls) == 2


# ── gh_json / gh_paginated ──────────────────────────────────────────────────


def test_gh_json_parses_stdout(monkeypatch) -> None:
    _install(monkeypatch, _ok('{"id": 7}'))
    assert _gh.gh_json(["api", "x"]) == {"id": 7}


def test_gh_json_raises_gh_error_on_non_json(monkeypatch) -> None:
    _install(monkeypatch, _ok("not json"))
    with pytest.raises(_gh.GhError, match="not JSON"):
        _gh.gh_json(["api", "x"])


def test_flatten_pages_bare_arrays() -> None:
    assert _gh.flatten_pages([[{"a": 1}, {"a": 2}], [], [{"a": 3}]]) == [
        {"a": 1},
        {"a": 2},
        {"a": 3},
    ]


def test_flatten_pages_keyed_objects() -> None:
    pages = [{"total_count": 3, "check_runs": [{"id": 1}]}, {"check_runs": [{"id": 2}]}]
    assert _gh.flatten_pages(pages, "check_runs") == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize(
    ("pages", "key"),
    [
        ({"check_runs": []}, "check_runs"),  # not slurped
        ([{"check_runs": [{"id": 1}]}, {"message": "oops"}], "check_runs"),  # page lost
        ([{"items": [{"id": 1}]}], None),  # wrapped page, no key given
        ([[{"id": 1}]], "check_runs"),  # bare page, key given
        ([[1, 2]], None),  # items that are not objects
    ],
)
def test_flatten_pages_refuses_a_page_of_the_wrong_shape(pages, key) -> None:
    with pytest.raises(ValueError):
        _gh.flatten_pages(pages, key)


def test_gh_paginated_slurps_and_flattens_every_page(monkeypatch) -> None:
    pages = [{"check_runs": [{"id": 1}, {"id": 2}]}, {"check_runs": [{"id": 3}]}]
    script = _install(monkeypatch, _ok(json.dumps(pages)))

    items = _gh.gh_paginated("repos/o/r/commits/s/check-runs", key="check_runs")

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]
    cmd = script.calls[0][0]
    assert cmd == [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/o/r/commits/s/check-runs",
    ]
    # Never a per-page --jq filter (docs/standards/ci.md).
    assert "--jq" not in cmd


def test_gh_paginated_raises_gh_error_on_a_bad_page(monkeypatch) -> None:
    _install(monkeypatch, _ok(json.dumps([{"message": "Not Found"}])))
    with pytest.raises(_gh.GhError, match="unexpected page shape"):
        _gh.gh_paginated("repos/o/r/commits/s/check-runs", key="check_runs")
