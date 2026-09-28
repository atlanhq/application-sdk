"""Tests for resolve_base_redirect.py — the GHCR base-image redirect preflight.

Two behaviours carry the weight here:

* a Dockerfile naming the public base reference is pinned to the GHCR digest,
  and a Dockerfile that cannot match is told so with a warning;
* only GHCR is ever resolved — the public reference is the registry gateway in
  front of the same package, so looking it up would only add a way to fail.

Everything else (unresolvable ARGs, an unreachable registry) must degrade to
building from the Dockerfile's own reference instead of blocking an app build.
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

PUBLIC = "registry.atlan.com/public/app-runtime-base"
GHCR = "ghcr.io/atlanhq/app-runtime-base"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def digests(ghcr: Optional[str]):
    """Build a ``resolve_digest`` stub returning a fixed GHCR digest.

    The public reference is served by the registry gateway from the same GHCR
    package, so there is nothing to compare it against: looking it up would only
    put the gateway back in the build path. The stub fails if asked.
    """

    def resolve(repo: str, _tag: str) -> Optional[str]:
        assert repo != PUBLIC, "the public reference must not be resolved"
        return ghcr

    return resolve


# ── Dockerfile parsing ────────────────────────────────────────────────────────


def test_plain_from_is_parsed():
    refs = rbr.parse_base_refs(f"FROM {PUBLIC}:3\nRUN echo hi\n")
    assert [(r.raw, r.line) for r in refs] == [(f"{PUBLIC}:3", 1)]
    assert not refs[0].unresolved


def test_global_arg_default_is_expanded():
    text = f"ARG BASE_IMAGE_TAG=3\nFROM {PUBLIC}:${{BASE_IMAGE_TAG}}\n"
    (ref,) = rbr.parse_base_refs(text)
    assert ref.resolved == f"{PUBLIC}:3"
    assert not ref.unresolved


def test_bare_dollar_arg_and_default_syntax_expand():
    text = (
        "ARG TAG=3\n"
        "ARG OTHER\n"
        f"FROM {PUBLIC}:$TAG AS one\n"
        f"FROM {PUBLIC}:${{MISSING:-3}} AS two\n"
    )
    resolved = [r.resolved for r in rbr.parse_base_refs(text)]
    assert resolved == [f"{PUBLIC}:3", f"{PUBLIC}:3"]


def test_arg_without_default_stays_unresolved():
    text = f"ARG BASE_IMAGE_TAG\nFROM {PUBLIC}:${{BASE_IMAGE_TAG}}\n"
    (ref,) = rbr.parse_base_refs(text)
    assert ref.unresolved


def test_arg_declared_after_first_from_does_not_expand_later_from():
    # BuildKit only lets ARGs declared before the first FROM reach FROM lines.
    text = f"FROM alpine AS a\nARG TAG=3\nFROM {PUBLIC}:${{TAG}}\n"
    refs = rbr.parse_base_refs(text)
    assert refs[-1].unresolved


def test_platform_flag_and_stage_alias_are_ignored():
    text = (
        f"FROM --platform=$BUILDPLATFORM {PUBLIC}:3 AS base\n"
        "FROM base AS final\n"
        "FROM BASE AS other\n"
    )
    refs = rbr.parse_base_refs(text)
    assert [r.resolved for r in refs] == [f"{PUBLIC}:3"]


def test_comments_and_continuations_are_handled():
    text = "# leading comment\n" f"FROM \\\n    {PUBLIC}:3 \\\n    AS base\n"
    (ref,) = rbr.parse_base_refs(text)
    assert ref.resolved == f"{PUBLIC}:3"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"{PUBLIC}:3", (PUBLIC, "3")),
        (PUBLIC, (PUBLIC, "latest")),
        (f"{PUBLIC}@{DIGEST_A}", (PUBLIC, DIGEST_A)),
        ("localhost:5000/base", ("localhost:5000/base", "latest")),
        ("localhost:5000/base:3", ("localhost:5000/base", "3")),
    ],
)
def test_split_ref(ref, expected):
    assert rbr.split_ref(ref) == expected


# ── Finding 1: match coverage ─────────────────────────────────────────────────


def test_non_matching_tag_builds_as_written():
    # Was fail-closed while the redirect was opt-in. With it on by default, a
    # base this script cannot rewrite is not the app's fault: warn, build from
    # the Dockerfile's reference, do not fail. (I001 rejects this tag on its own
    # account.)
    refs = rbr.parse_base_refs(f"FROM {PUBLIC}:3.26.1\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == ""
    assert "cannot be redirected" in decision.warnings[0]


def test_digest_pinned_base_builds_as_written():
    # The case that forced this change: I001 *approves* a digest-pinned base,
    # and it cannot match a tag mapping. Fail-closed here would have broken
    # every such app's build the moment the default turned on.
    refs = rbr.parse_base_refs(f"FROM {PUBLIC}:3@{DIGEST_A}\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == ""


def test_already_on_ghcr_is_a_noop_not_an_error():
    # Once I001 accepts the mirror, a Dockerfile may name GHCR directly. There is
    # nothing to redirect and nothing wrong: the build already pulls from GHCR.
    refs = rbr.parse_base_refs(f"FROM {GHCR}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == ""
    assert any("already resolves" in n for n in decision.notes)


def test_already_on_ghcr_digest_pinned_is_a_noop():
    refs = rbr.parse_base_refs(f"FROM {GHCR}@{DIGEST_A}\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == ""


def test_already_on_ghcr_with_another_tag_is_still_a_noop():
    # Whether the tag is one I001 accepts is I001's business; the redirect only
    # asks "is there a public reference to rewrite" -- and there is not.
    refs = rbr.parse_base_refs(f"FROM {GHCR}:3.26.1\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == ""


def test_public_stage_is_redirected_even_when_another_stage_names_ghcr():
    text = f"FROM {GHCR}:3 AS tools\nFROM {PUBLIC}:3 AS app\n"
    refs = rbr.parse_base_refs(text)
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == f"{PUBLIC}:3=docker-image://{GHCR}@{DIGEST_A}"


def test_unresolvable_reference_warns_and_skips_redirect():
    refs = rbr.parse_base_refs(f"ARG T\nFROM {PUBLIC}:${{T}}\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts == ""
    assert "only inside BuildKit" in decision.warnings[0]


def test_match_alongside_other_stages_is_found():
    text = f"FROM ghcr.io/astral-sh/uv:latest AS uv\nFROM {PUBLIC}:3 AS app\n"
    decision = rbr.decide(rbr.parse_base_refs(text), resolve_digest=digests(DIGEST_A))
    assert decision.build_contexts.startswith(f"{PUBLIC}:3=docker-image://{GHCR}@")


# ── Digest pin ──────────────────────────────────────────────────────────────────


def test_match_pins_the_immutable_ghcr_digest():
    refs = rbr.parse_base_refs(f"FROM {PUBLIC}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(DIGEST_A))
    assert decision.digest == DIGEST_A
    assert decision.build_contexts == f"{PUBLIC}:3=docker-image://{GHCR}@{DIGEST_A}"
    # The mutable tag must not appear on the right-hand side.
    assert f"{GHCR}:3" not in decision.build_contexts


def test_unresolvable_ghcr_degrades_to_the_public_reference():
    refs = rbr.parse_base_refs(f"FROM {PUBLIC}:3\n")
    decision = rbr.decide(refs, resolve_digest=digests(None))
    assert decision.build_contexts == ""
    assert decision.warnings


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
    assert rbr.registry_digest(PUBLIC, "3") == DIGEST_A
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
        (PUBLIC, "registry.atlan.com"),
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
    dockerfile = _write_dockerfile(tmp_path, f"FROM {PUBLIC}:3\n")
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
    assert rbr.registry_digest(PUBLIC, "3") == DIGEST_A


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
    assert rbr.registry_digest(PUBLIC, "3") is None


# ── CLI wiring ────────────────────────────────────────────────────────────────


def _write_dockerfile(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text(text, encoding="utf-8")
    return path


def test_main_writes_outputs_on_success(tmp_path, monkeypatch, capsys):
    dockerfile = _write_dockerfile(tmp_path, f"FROM {PUBLIC}:3\n")
    output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(rbr, "registry_digest", lambda repo, tag, **kwargs: DIGEST_A)

    assert rbr.main(["--dockerfile", str(dockerfile)]) == 0
    written = output.read_text(encoding="utf-8")
    assert f"build_contexts={PUBLIC}:3=docker-image://{GHCR}@{DIGEST_A}" in written
    assert f"base_digest={DIGEST_A}" in written


def test_main_succeeds_when_the_public_reference_is_unreachable(tmp_path, monkeypatch):
    """The case that used to fail every app build: the parity gate resolved the
    public reference and failed closed when it could not."""
    dockerfile = _write_dockerfile(tmp_path, f"FROM {PUBLIC}:3\n")
    monkeypatch.setattr(
        rbr,
        "registry_digest",
        lambda repo, tag, **kwargs: DIGEST_A if repo == GHCR else None,
    )
    assert rbr.main(["--dockerfile", str(dockerfile)]) == 0


def test_main_exits_nonzero_on_unreadable_dockerfile(tmp_path, capsys):
    assert rbr.main(["--dockerfile", str(tmp_path / "nope")]) == 1
    assert "Cannot read Dockerfile" in capsys.readouterr().out


def test_main_resolves_only_ghcr(tmp_path, monkeypatch):
    dockerfile = _write_dockerfile(tmp_path, f"FROM {PUBLIC}:3\n")
    monkeypatch.setenv("GHCR_TOKEN", "pat-value")
    monkeypatch.setenv("GHCR_USER", "actor")
    seen: list[tuple[str, str, str]] = []

    def fake_digest(repo, tag, *, user="", token=""):
        seen.append((repo, user, token))
        return DIGEST_A

    monkeypatch.setattr(rbr, "registry_digest", fake_digest)
    assert rbr.main(["--dockerfile", str(dockerfile)]) == 0

    # Only GHCR is looked up: the public reference is the gateway in front of
    # the same package, so resolving it would add a dependency and prove nothing.
    assert seen == [(GHCR, "actor", "pat-value")]
