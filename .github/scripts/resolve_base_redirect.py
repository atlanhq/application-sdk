#!/usr/bin/env python3
"""Resolve the GHCR base-image redirect for an opted-in app build.

Emits the ``build-contexts`` mapping that redirects the SDK base image from
Harbor to GHCR, after proving the redirect is both *applicable* and *safe*:

1. **Match coverage.** BuildKit's named-context substitution is reference
   specific: it only fires when the Dockerfile's ``FROM`` reference is exactly
   the mapping's left-hand side. A caller that opts in but pins another tag
   would silently keep pulling from Harbor and still go green. This script
   parses the Dockerfile (expanding global ``ARG`` defaults the way BuildKit
   does) and **fails closed** when it can prove no ``FROM`` matches the
   supported reference. When a base reference cannot be resolved statically
   (an ``ARG`` with no default), it warns and emits no mapping rather than
   blocking a build it cannot reason about.

2. **Cross-registry parity.** ``harbor-release.yaml`` pushes both registries
   from one buildx invocation, but the push is not transactional: a GHCR-leg
   failure after the Harbor names land leaves GHCR's floating ``:3`` on a
   stale digest. This script resolves the tag on *both* registries and fails
   closed on skew, pointing at the documented re-run recovery. On parity it
   pins the named context to the **immutable digest** rather than the mutable
   tag, so the build cannot race a concurrent base release.

Registry unavailability is not skew — but it is not always a degrade either.
If GHCR cannot be resolved the script warns and emits an empty mapping,
degrading to the pre-redirect behaviour (pull from Harbor) instead of failing
the app build. If *Harbor* cannot be resolved there is no working baseline to
verify parity against, so the script fails closed — an unverified redirect is
indistinguishable from a stale one.

Environment:
    GHCR_TOKEN     Token for ghcr.io registry auth (optional; anonymous when unset)
    GHCR_USER      Username paired with GHCR_TOKEN (default: ``x-access-token``)
    GITHUB_OUTPUT  Path to the step output file (optional when run locally)

Usage (from within a workflow step)::

    python3 .github/scripts/resolve_base_redirect.py --dockerfile ./Dockerfile

Writes ``build_contexts`` (the mapping, or empty) and ``base_digest`` to
``$GITHUB_OUTPUT``.

See ``docs/standards/build-security.md`` for the two-registry layout and the
partial-publish recovery, and ``docs/standards/ci.md`` for why this logic lives
in a tested script rather than inline workflow shell.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

# The base image, as apps reference it (Harbor) and as CI retrieves it (GHCR).
HARBOR_REPO = "registry.atlan.com/public/app-runtime-base"
GHCR_REPO = "ghcr.io/atlanhq/app-runtime-base"

# Registry host that gets a credential. Compared against the parsed host, never
# matched as a URL substring or prefix — `ghcr.io.example.com/x` must not read as
# GHCR just because the string starts the same way.
GHCR_HOST = "ghcr.io"

# Tags the redirect supports. Deliberately narrow: `refactor-v3-latest` is a
# workflow_dispatch branch build, so redirecting it would change *which* image a
# repo resolves to, not just where the layers come from. Widening this set means
# confirming harbor-release.yaml publishes the tag to both registries.
SUPPORTED_TAGS = ("3",)

# Manifest media types to accept — the base is a multi-arch index, but accept the
# single-manifest types too so a degenerate publish still resolves.
_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

_HTTP_TIMEOUT_S = 20

# Seam for tests: monkeypatch this rather than the stdlib.
_urlopen: Callable[..., object] = urllib.request.urlopen


# ── Dockerfile parsing ────────────────────────────────────────────────────────


@dataclass
class BaseRef:
    """One external ``FROM`` reference found in a Dockerfile."""

    raw: str
    """The reference exactly as written, before ARG expansion."""

    resolved: str
    """The reference after expanding global ARG defaults."""

    line: int
    """1-indexed line number of the ``FROM`` instruction."""

    @property
    def unresolved(self) -> bool:
        """True when expansion left a variable reference behind."""
        return "$" in self.resolved


def _strip_continuations(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued lines, dropping comments and blanks.

    Returns:
        ``(line_number, logical_line)`` pairs, where ``line_number`` is the
        1-indexed line the logical line *started* on.
    """
    logical: list[tuple[int, str]] = []
    buf = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        # A comment line inside a continuation is ignored by BuildKit too.
        if not stripped or stripped.startswith("#"):
            continue
        if not buf:
            start = lineno
        if stripped.endswith("\\"):
            buf += stripped[:-1].strip() + " "
            continue
        buf += stripped
        logical.append((start, buf))
        buf = ""
    if buf:
        logical.append((start, buf))
    return logical


_ARG_RE = re.compile(r"^ARG\s+(?P<rest>.+)$", re.IGNORECASE)
_FROM_RE = re.compile(r"^FROM\s+(?P<rest>.+)$", re.IGNORECASE)
_VAR_RE = re.compile(r"\$(?:\{(?P<braced>[^}]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))")


def _expand(ref: str, args: dict[str, str]) -> str:
    """Expand ``$VAR``, ``${VAR}`` and ``${VAR:-default}`` against *args*.

    Unknown variables are left as written so the caller can tell a resolved
    reference from one that only BuildKit can settle.
    """

    def sub(match: re.Match[str]) -> str:
        braced = match.group("braced")
        if braced is None:
            name = match.group("bare")
            return args.get(name, match.group(0))
        if ":-" in braced:
            name, _, default = braced.partition(":-")
            return args.get(name.strip(), default)
        if ":+" in braced or ":" in braced:
            # Alternate-value / substring forms: not worth emulating, and a
            # wrong guess is worse than admitting we do not know.
            return match.group(0)
        return args.get(braced.strip(), match.group(0))

    return _VAR_RE.sub(sub, ref)


def parse_base_refs(text: str) -> list[BaseRef]:
    """Extract the external base references a Dockerfile builds ``FROM``.

    Stage aliases (``FROM builder AS final``) and ``--platform`` flags are
    filtered out, and global ``ARG`` defaults are expanded — matching BuildKit,
    where only ARGs declared *before* the first ``FROM`` are usable in ``FROM``
    instructions.

    Args:
        text: Full Dockerfile contents.

    Returns:
        One :class:`BaseRef` per external ``FROM``, in file order.
    """
    global_args: dict[str, str] = {}
    stages: set[str] = set()
    refs: list[BaseRef] = []
    seen_from = False

    for lineno, line in _strip_continuations(text):
        arg_match = _ARG_RE.match(line)
        if arg_match and not seen_from:
            for token in arg_match.group("rest").split():
                name, sep, value = token.partition("=")
                if sep:
                    global_args[name] = value.strip("\"'")
            continue

        from_match = _FROM_RE.match(line)
        if not from_match:
            continue
        seen_from = True

        tokens = from_match.group("rest").split()
        # Drop flags (--platform=..., --chmod=...) and the trailing `AS <name>`.
        image_tokens = [t for t in tokens if not t.startswith("--")]
        if not image_tokens:
            continue
        raw = image_tokens[0]
        if len(image_tokens) >= 3 and image_tokens[1].upper() == "AS":
            stages.add(image_tokens[2].lower())

        # `FROM builder` / `FROM 0` reference an earlier stage, not a registry.
        if raw.lower() in stages or raw.isdigit():
            continue

        refs.append(BaseRef(raw=raw, resolved=_expand(raw, global_args), line=lineno))

    return refs


def split_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into ``(repository, tag)``.

    Digest references (``repo@sha256:…``) return the digest as the tag. A
    reference with no tag returns ``latest``, matching Docker's default.
    """
    if "@" in ref:
        repo, _, digest = ref.partition("@")
        return repo, digest
    # Only the last path segment may carry a tag — a registry port colon
    # (host:5000/repo) must not be mistaken for one.
    repo, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        return ref, "latest"
    return repo, tag


# ── Registry digest resolution ────────────────────────────────────────────────


def registry_host(repo: str) -> str:
    """Return the registry host of a repository reference.

    ``ghcr.io/atlanhq/app-runtime-base`` -> ``ghcr.io``. Callers compare the
    result for equality; deciding "is this GHCR?" from a substring or prefix
    test on the whole reference would also accept lookalikes such as
    ``ghcr.io.example.com/x``.
    """
    return repo.partition("/")[0].lower()


def _basic_auth(user: str, token: str) -> str:
    raw = f"{user}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _parse_challenge(header: str) -> dict[str, str]:
    """Parse a ``WWW-Authenticate: Bearer realm="…",service="…"`` header."""
    if not header.lower().startswith("bearer"):
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def _bearer_token(
    challenge: dict[str, str],
    user: str,
    token: str,
    *,
    expected_host: str,
) -> Optional[str]:
    """Exchange registry credentials for a pull-scoped bearer token.

    The realm is attacker-influenced in principle — it arrives in the registry's
    ``WWW-Authenticate`` response header — and this request is the only place a
    credential leaves the runner. So the realm must be HTTPS, and when a
    credential would be attached, its host must match the registry we set out to
    query (true for both registries here: ``ghcr.io/token`` and
    ``registry.atlan.com/service/token``). A redirected realm therefore costs a
    digest lookup, never the token.
    """
    realm = challenge.get("realm")
    if not realm:
        return None
    parsed = urllib.parse.urlsplit(realm)
    if parsed.scheme != "https":
        print(f"  refusing non-HTTPS token realm: {parsed.scheme}://…", flush=True)
        return None
    if token and parsed.hostname != expected_host:
        print(
            f"  refusing to send credentials to token realm {parsed.hostname} "
            f"(expected {expected_host})",
            flush=True,
        )
        return None
    params = {k: v for k, v in challenge.items() if k in ("service", "scope") and v}
    url = f"{realm}?{urllib.parse.urlencode(params)}" if params else realm
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", _basic_auth(user, token))
    with _urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:  # type: ignore[operator]
        payload = json.loads(response.read().decode())
    return payload.get("token") or payload.get("access_token")


def registry_digest(
    repo: str,
    tag: str,
    *,
    user: str = "",
    token: str = "",
) -> Optional[str]:
    """Resolve the manifest digest a registry currently serves for *tag*.

    Performs the standard Docker Registry v2 token dance: an unauthenticated
    request first, then a bearer-token retry if the registry challenges.

    Args:
        repo: Full repository reference, e.g. ``ghcr.io/atlanhq/app-runtime-base``.
        tag: Tag to resolve.
        user: Username for the token exchange (ignored when *token* is empty).
        token: Registry credential. Empty means anonymous.

    Returns:
        The ``sha256:…`` digest, or ``None`` when the tag or registry could not
        be reached (missing tag, auth failure, network error). Callers treat
        ``None`` as *unknown*, never as *skew*.
    """
    host = registry_host(repo)
    path = repo.partition("/")[2]
    url = f"https://{host}/v2/{path}/manifests/{urllib.parse.quote(tag)}"

    def fetch(auth: Optional[str]) -> Optional[str]:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", _ACCEPT)
        if auth:
            request.add_header("Authorization", auth)
        with _urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:  # type: ignore[operator]
            digest = response.headers.get("Docker-Content-Digest")
            if digest:
                return digest
            # Registries may omit the header; fall back to hashing the body.
            return "sha256:" + hashlib.sha256(response.read()).hexdigest()

    try:
        return fetch(None)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            print(f"  {repo}:{tag} -> HTTP {exc.code}", flush=True)
            return None
        challenge = _parse_challenge(exc.headers.get("WWW-Authenticate", ""))
    except (urllib.error.URLError, OSError) as exc:
        print(f"  {repo}:{tag} -> unreachable ({exc})", flush=True)
        return None

    try:
        bearer = _bearer_token(
            challenge, user or "x-access-token", token, expected_host=host
        )
        if not bearer:
            print(f"  {repo}:{tag} -> no token from registry challenge", flush=True)
            return None
        return fetch(f"Bearer {bearer}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  {repo}:{tag} -> auth/resolve failed ({exc})", flush=True)
        return None


# ── Decision ──────────────────────────────────────────────────────────────────


@dataclass
class Decision:
    """Outcome of the preflight: what to emit, and what to say about it."""

    build_contexts: str = ""
    digest: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def decide(
    refs: list[BaseRef],
    *,
    harbor_repo: str = HARBOR_REPO,
    ghcr_repo: str = GHCR_REPO,
    supported_tags: tuple[str, ...] = SUPPORTED_TAGS,
    resolve_digest: Callable[[str, str], Optional[str]] = lambda repo, tag: None,
) -> Decision:
    """Decide the ``build-contexts`` value for an opted-in build.

    Args:
        refs: Base references parsed from the caller's Dockerfile.
        harbor_repo: Repository apps reference in their ``FROM``.
        ghcr_repo: Repository CI should retrieve the layers from.
        supported_tags: Tags the redirect is allowed to rewrite.
        resolve_digest: ``(repo, tag) -> digest | None`` — injected so the
            decision logic is testable without network access.

    Returns:
        A :class:`Decision`. ``errors`` non-empty means fail the build.
    """
    decision = Decision()

    supported_refs = {(harbor_repo, tag) for tag in supported_tags}
    matched = [ref for ref in refs if split_ref(ref.resolved) in supported_refs]

    if not matched:
        listed = ", ".join(f"{r.raw} (line {r.line})" for r in refs) or "none"
        unresolved = [r for r in refs if r.unresolved]
        supported = ", ".join(f"{harbor_repo}:{t}" for t in supported_tags)
        if unresolved:
            decision.warnings.append(
                f"use_ghcr_base is set, but no FROM statically matches {supported}, "
                f"and {len(unresolved)} reference(s) resolve only inside BuildKit "
                f"({', '.join(r.raw for r in unresolved)}). Building from Harbor "
                "unchanged. Confirm the base tag or pass it as a Dockerfile ARG "
                "default so this check can see it."
            )
            return decision
        decision.errors.append(
            f"use_ghcr_base is set, but no FROM in this Dockerfile references "
            f"{supported}, so the redirect would be a silent no-op and the build "
            f"would still pull from Harbor. Found: {listed}. Either repin the base "
            "to a supported reference or unset use_ghcr_base."
        )
        return decision

    tag = split_ref(matched[0].resolved)[1]
    harbor_digest = resolve_digest(harbor_repo, tag)
    ghcr_digest = resolve_digest(ghcr_repo, tag)

    if ghcr_digest is None:
        decision.warnings.append(
            f"{ghcr_repo}:{tag} could not be resolved, so the redirect is skipped "
            "and this build pulls from Harbor as before. If this persists, check "
            "that harbor-release.yaml published the GHCR leg."
        )
        return decision

    if harbor_digest is None:
        # Harbor is the redirect's *source*: when it cannot be resolved there is
        # nothing to verify the GHCR tag against, so parity is unknowable and
        # the pinned GHCR digest could be the stale leg of a partial publish.
        # Degrading to a Harbor pull is not an option either — Harbor is the
        # unreachable side — so the parity gate fails closed here, exactly as it
        # does on proven skew. Only GHCR-unresolvable degrades (above).
        decision.errors.append(
            f"{harbor_repo}:{tag} could not be resolved, so cross-registry "
            "parity cannot be verified — this build would ride the GHCR "
            "redirect on an unproven base. Harbor unreachable is treated like "
            "skew: re-run once Harbor recovers. Unsetting use_ghcr_base does "
            "not help while Harbor is down — it moves the failure from this "
            "check to the base-image pull. See docs/standards/build-security.md."
        )
        return decision

    if harbor_digest != ghcr_digest:
        decision.errors.append(
            f"Cross-registry digest skew on :{tag} — {harbor_repo} serves "
            f"{harbor_digest} but {ghcr_repo} serves {ghcr_digest}. The base "
            "publish is not transactional across registries, so a GHCR-leg failure "
            "can leave the floating tag stale. Re-run the failed harbor-release "
            "run (do not cut a new release) to restore parity — see "
            "docs/standards/build-security.md."
        )
        return decision

    decision.digest = ghcr_digest
    # Pin the immutable digest, not the mutable tag: a base release landing
    # mid-build cannot change what this build resolves to, and the digest is the
    # one just verified equal to Harbor's.
    decision.build_contexts = (
        f"{harbor_repo}:{tag}=docker-image://{ghcr_repo}@{ghcr_digest}"
    )
    return decision


# ── CLI ───────────────────────────────────────────────────────────────────────


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dockerfile",
        default="Dockerfile",
        help="Path to the Dockerfile the build will use.",
    )
    parser.add_argument(
        "--harbor-repo",
        default=HARBOR_REPO,
        help="Repository apps reference in their FROM (redirect source).",
    )
    parser.add_argument(
        "--ghcr-repo",
        default=GHCR_REPO,
        help="Repository CI retrieves the base layers from (redirect target).",
    )
    args = parser.parse_args(argv)

    try:
        text = open(args.dockerfile, encoding="utf-8").read()
    except OSError as exc:
        print(f"::error::Cannot read Dockerfile {args.dockerfile}: {exc}")
        return 1

    refs = parse_base_refs(text)
    print(f"Base references in {args.dockerfile}:")
    for ref in refs:
        suffix = "" if ref.raw == ref.resolved else f" -> {ref.resolved}"
        print(f"  line {ref.line}: {ref.raw}{suffix}")

    token = os.environ.get("GHCR_TOKEN", "")
    user = os.environ.get("GHCR_USER", "x-access-token")

    def resolve_digest(repo: str, tag: str) -> Optional[str]:
        # Host equality, not a prefix test on the reference: only the real GHCR
        # gets the credential. Harbor's public project is pulled anonymously.
        is_ghcr = registry_host(repo) == GHCR_HOST
        digest = registry_digest(
            repo,
            tag,
            user=user if is_ghcr else "",
            token=token if is_ghcr else "",
        )
        print(f"  {repo}:{tag} -> {digest or 'unresolved'}", flush=True)
        return digest

    decision = decide(
        refs,
        harbor_repo=args.harbor_repo,
        ghcr_repo=args.ghcr_repo,
        resolve_digest=resolve_digest,
    )

    for warning in decision.warnings:
        print(f"::warning::{warning}")
    for error in decision.errors:
        print(f"::error::{error}")

    _write_output("build_contexts", decision.build_contexts)
    _write_output("base_digest", decision.digest)

    if not decision.ok:
        return 1
    if decision.build_contexts:
        print(f"Redirect active: {decision.build_contexts}")
    else:
        print("Redirect inactive: building from Harbor as before.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
