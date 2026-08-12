"""Tests for resolve_base_redirect.py — the GHCR base-image redirect preflight.

Two behaviours carry the weight here, both fail-closed:

* a caller that opts in but whose Dockerfile cannot match the redirect must be
  told, not silently built from Harbor;
* a cross-registry digest skew must stop the build rather than produce a green
  build on a stale base.

Everything else (unresolvable ARGs, an unreachable registry) must degrade to the
pre-redirect behaviour instead of blocking an app build.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Optional

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "resolve_base_redirect.py"
_spec = importlib.util.spec_from_file_location("resolve_base_redirect", SCRIPT)
assert _spec and _spec.loader
rbr = importlib.util.module_from_spec(_spec)
sys.modules["resolve_base_redirect"] = rbr
_spec.loader.exec_module(rbr)

HARBOR = "registry.atlan.com/public/app-runtime-base"
GHCR = "ghcr.io/atlanhq/app-runtime-base"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def digests(harbor: Optional[str], ghcr: Optional[str]):
    """Build a ``resolve_digest`` stub returning fixed digests per registry."""

    def resolve(repo: str, _tag: str) -> Optional[str]:
        return harbor if repo == HARBOR else ghcr

    return resolve


# ── Dockerfile parsing ────────────────────────────────────────────────────────


def test_plain_from_is_parsed():
    refs = rbr.parse_base_refs(f"FROM {HARBOR}:3\nRUN echo hi\n")
    assert [(r.raw, r.line) for r in refs] == [(f"{HARBOR}:3", 1)]
    assert not refs[0].unresolved


def test_global_arg_default_is_expanded():
    text = f"ARG BASE_IMAGE_TAG=3\nFROM {HARBOR}:${{BASE_IMAGE_TAG}}\n"
    (ref,) = rbr.parse_base_refs(text)
    assert ref.resolved == f"{HARBOR}:3"
    assert not ref.unresolved


def test_bare_dollar_arg_and_default_syntax_expand():
    text = (
        "ARG TAG=3\n"
        "ARG OTHER\n"
        f"FROM {HARBOR}:$TAG AS one\n"
        f"FROM {HARBOR}:${{MISSING:-3}} AS two\n"
    )
    resolved = [r.resolved for r in rbr.parse_base_refs(text)]
    assert resolved == [f"{HARBOR}:3", f"{HARBOR}:3"]


def test_arg_without_default_stays_unresolved():
    text = f"ARG BASE_IMAGE_TAG\nFROM {HARBOR}:${{BASE_IMAGE_TAG}}\n"
    (ref,) = rbr.parse_base_refs(text)
    assert ref.unresolved


def test_arg_declared_after_first_from_does_not_expand_later_from():
    # BuildKit only lets ARGs declared before the first FROM reach FROM lines.
    text = f"FROM alpine AS a\nARG TAG=3\nFROM {HARBOR}:${{TAG}}\n"
    refs = rbr.parse_base_refs(text)
    assert refs[-1].unresolved


def test_platform_flag_and_stage_alias_are_ignored():
    text = (
        f"FROM --platform=$BUILDPLATFORM {HARBOR}:3 AS base\n"
        "FROM base AS final\n"
        "FROM BASE AS other\n"
    )
    refs = rbr.parse_base_refs(text)
    assert [r.resolved for r in refs] == [f"{HARBOR}:3"]


def test_comments_and_continuations_are_handled():
    text = "# leading comment\n" f"FROM \\\n    {HARBOR}:3 \\\n    AS base\n"
    (ref,) = rbr.parse_base_refs(text)
    assert ref.resolved == f"{HARBOR}:3"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"{HARBOR}:3", (HARBOR, "3")),
        (HARBOR, (HARBOR, "latest")),
        (f"{HARBOR}@{DIGEST_A}", (HARBOR, DIGEST_A)),
        ("localhost:5000/base", ("localhost:5000/base", "latest")),
        ("localhost:5000/base:3", ("localhost:5000/base", "3")),
    ],
)
def test_split_ref(ref, expected):
    assert rbr.split_ref(ref) == expected


# ── Finding 1: match coverage ─────────────────────────────────────────────────


def test_non_matching_tag_fails_closed():
    refs = rbr.parse_base_refs(f"FROM {HARBOR}:3.26.1\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A, DIGEST_A))
    assert not decision.ok
    assert decision.build_contexts == ""
    assert "silent no-op" in decision.errors[0]


def test_digest_pinned_base_fails_closed():
    refs = rbr.parse_base_refs(f"FROM {HARBOR}@{DIGEST_A}\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A, DIGEST_A))
    assert not decision.ok


def test_unresolvable_reference_warns_and_skips_redirect():
    refs = rbr.parse_base_refs(f"ARG T\nFROM {HARBOR}:${{T}}\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A, DIGEST_A))
    assert decision.ok
    assert decision.build_contexts == ""
    assert "only inside BuildKit" in decision.warnings[0]


def test_match_alongside_other_stages_is_found():
    text = f"FROM ghcr.io/astral-sh/uv:latest AS uv\nFROM {HARBOR}:3 AS app\n"
    decision = rbr.decide(
        rbr.parse_base_refs(text), resolve_digest=digests(DIGEST_A, DIGEST_A)
    )
    assert decision.ok
    assert decision.build_contexts.startswith(f"{HARBOR}:3=docker-image://{GHCR}@")


# ── Finding 2: cross-registry parity ──────────────────────────────────────────


def test_parity_pins_the_immutable_digest():
    refs = rbr.parse_base_refs(f"FROM {HARBOR}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A, DIGEST_A))
    assert decision.ok
    assert decision.digest == DIGEST_A
    assert decision.build_contexts == f"{HARBOR}:3=docker-image://{GHCR}@{DIGEST_A}"
    # The mutable tag must not appear on the right-hand side.
    assert f"{GHCR}:3" not in decision.build_contexts


def test_digest_skew_fails_closed():
    refs = rbr.parse_base_refs(f"FROM {HARBOR}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A, DIGEST_B))
    assert not decision.ok
    assert decision.build_contexts == ""
    assert "skew" in decision.errors[0]
    assert "harbor-release" in decision.errors[0]


def test_unresolvable_ghcr_degrades_to_harbor():
    refs = rbr.parse_base_refs(f"FROM {HARBOR}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A, None))
    assert decision.ok
    assert decision.build_contexts == ""
    assert decision.warnings


def test_unresolvable_harbor_fails_closed():
    # Harbor is the redirect's source: with it unreachable there is no parity
    # baseline, so an unverified redirect is treated like a stale one. Only
    # GHCR-unreachable degrades.
    refs = rbr.parse_base_refs(f"FROM {HARBOR}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(None, DIGEST_A))
    assert not decision.ok
    assert decision.build_contexts == ""
    assert "parity cannot be verified" in decision.errors[0]
    # The remedy is a re-run, and the message must say so without offering the
    # flag as a way out: with Harbor down, building without the redirect just
    # moves the failure to the base-image pull.
    assert "re-run once Harbor recovers" in decision.errors[0]
    assert "does not help" in decision.errors[0]


# ── Registry client ───────────────────────────────────────────────────────────


class _Response:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def route(request) -> tuple[str, str]:
    """Split a fake request into ``(host, path)``.

    The fake registries below dispatch on this rather than on ``in`` tests
    against the whole URL: a substring match would let ``/v2/`` or a host name
    appearing anywhere in the URL pick the branch, which is the same defect
    these tests exist to pin down in the code under test.
    """
    parts = urllib.parse.urlsplit(request.full_url)
    return parts.hostname or "", parts.path


def test_registry_digest_reads_content_digest_header(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["accept"] = request.get_header("Accept")
        return _Response({"Docker-Content-Digest": DIGEST_A})

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(HARBOR, "3") == DIGEST_A
    assert (
        seen["url"]
        == "https://registry.atlan.com/v2/public/app-runtime-base/manifests/3"
    )
    assert "oci.image.index" in seen["accept"]


def test_registry_digest_performs_token_dance(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request.full_url, request.get_header("Authorization")))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {
                    "WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'
                },
                io.BytesIO(b""),
            )
        if route(request) == ("ghcr.io", "/token"):
            return _Response(body=b'{"token": "tok"}')
        return _Response({"Docker-Content-Digest": DIGEST_B})

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(GHCR, "3", user="u", token="pat") == DIGEST_B
    # Token request carries basic auth; the manifest retry carries the bearer.
    assert calls[1][1].startswith("Basic ")
    assert calls[2][1] == "Bearer tok"


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        (GHCR, "ghcr.io"),
        ("GHCR.IO/atlanhq/app-runtime-base", "ghcr.io"),
        (HARBOR, "registry.atlan.com"),
        # Lookalikes a prefix/substring test on the reference would wave through.
        ("ghcr.io.example.com/atlanhq/app-runtime-base", "ghcr.io.example.com"),
        ("evil.example.com/ghcr.io/app-runtime-base", "evil.example.com"),
    ],
)
def test_registry_host_parses_rather_than_matches_substrings(repo, expected):
    assert rbr.registry_host(repo) == expected
    assert (rbr.registry_host(repo) == rbr.GHCR_HOST) is (expected == "ghcr.io")


def test_credentials_are_withheld_from_a_ghcr_lookalike_host(tmp_path, monkeypatch):
    lookalike = "ghcr.io.example.com/atlanhq/app-runtime-base"
    dockerfile = _write_dockerfile(tmp_path, f"FROM {HARBOR}:3\n")
    monkeypatch.setenv("GHCR_TOKEN", "pat-value")
    monkeypatch.setenv("GHCR_USER", "actor")
    seen: list[tuple[str, str]] = []

    def fake_digest(repo, tag, *, user="", token=""):
        seen.append((repo, token))
        return DIGEST_A

    monkeypatch.setattr(rbr, "registry_digest", fake_digest)
    rbr.main(["--dockerfile", str(dockerfile), "--ghcr-repo", lookalike])

    assert dict(seen)[lookalike] == ""


def test_token_realm_on_another_host_does_not_receive_credentials(monkeypatch):
    sent: list[tuple[str, Optional[str]]] = []

    def fake_urlopen(request, timeout=None):
        host, path = route(request)
        if path.startswith("/v2/"):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {
                    "WWW-Authenticate": 'Bearer realm="https://evil.example.com/token",service="ghcr.io"'
                },
                io.BytesIO(b""),
            )
        sent.append((host, request.get_header("Authorization")))
        return _Response(body=b'{"token": "tok"}')

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(GHCR, "3", user="u", token="pat") is None
    # The off-host realm was never contacted at all, so the PAT never left.
    assert sent == []


def test_non_https_token_realm_is_refused(monkeypatch):
    def fake_urlopen(request, timeout=None):
        if route(request)[1].startswith("/v2/"):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {"WWW-Authenticate": 'Bearer realm="http://ghcr.io/token"'},
                io.BytesIO(b""),
            )
        raise AssertionError("plaintext realm must not be contacted")

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(GHCR, "3", user="u", token="pat") is None


def test_anonymous_lookup_tolerates_a_delegated_realm(monkeypatch):
    # With no credential to protect, a realm on another host is fine — some
    # registries genuinely delegate their token service.
    def fake_urlopen(request, timeout=None):
        host, path = route(request)
        if path.startswith("/v2/") and not request.get_header("Authorization"):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "unauthorized",
                {"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token"'},
                io.BytesIO(b""),
            )
        if host == "auth.example.com":
            return _Response(body=b'{"token": "tok"}')
        return _Response({"Docker-Content-Digest": DIGEST_A})

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(HARBOR, "3") == DIGEST_A


def test_registry_digest_returns_none_on_missing_tag(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 404, "not found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(GHCR, "3") is None


def test_registry_digest_returns_none_when_unreachable(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(rbr, "_urlopen", fake_urlopen)
    assert rbr.registry_digest(HARBOR, "3") is None


# ── CLI wiring ────────────────────────────────────────────────────────────────


def _write_dockerfile(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text(text, encoding="utf-8")
    return path


def test_main_writes_outputs_on_success(tmp_path, monkeypatch, capsys):
    dockerfile = _write_dockerfile(tmp_path, f"FROM {HARBOR}:3\n")
    output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(rbr, "registry_digest", lambda repo, tag, **kwargs: DIGEST_A)

    assert rbr.main(["--dockerfile", str(dockerfile)]) == 0
    written = output.read_text(encoding="utf-8")
    assert f"build_contexts={HARBOR}:3=docker-image://{GHCR}@{DIGEST_A}" in written
    assert f"base_digest={DIGEST_A}" in written


def test_main_exits_nonzero_and_annotates_on_skew(tmp_path, monkeypatch, capsys):
    dockerfile = _write_dockerfile(tmp_path, f"FROM {HARBOR}:3\n")
    output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        rbr,
        "registry_digest",
        lambda repo, tag, **kwargs: DIGEST_A if repo == HARBOR else DIGEST_B,
    )

    assert rbr.main(["--dockerfile", str(dockerfile)]) == 1
    assert "::error::" in capsys.readouterr().out
    # An empty mapping is still written so a consuming step never sees a stale value.
    assert "build_contexts=\n" in output.read_text(encoding="utf-8")


def test_main_exits_nonzero_on_unreadable_dockerfile(tmp_path, capsys):
    assert rbr.main(["--dockerfile", str(tmp_path / "nope")]) == 1
    assert "Cannot read Dockerfile" in capsys.readouterr().out


def test_main_sends_credentials_only_to_ghcr(tmp_path, monkeypatch):
    dockerfile = _write_dockerfile(tmp_path, f"FROM {HARBOR}:3\n")
    monkeypatch.setenv("GHCR_TOKEN", "pat-value")
    monkeypatch.setenv("GHCR_USER", "actor")
    seen: list[tuple[str, str, str]] = []

    def fake_digest(repo, tag, *, user="", token=""):
        seen.append((repo, user, token))
        return DIGEST_A

    monkeypatch.setattr(rbr, "registry_digest", fake_digest)
    assert rbr.main(["--dockerfile", str(dockerfile)]) == 0

    by_repo = {repo: (user, token) for repo, user, token in seen}
    assert by_repo[GHCR] == ("actor", "pat-value")
    # The Harbor leg is anonymous — the PAT must not leave GitHub's registry.
    assert by_repo[HARBOR] == ("", "")
