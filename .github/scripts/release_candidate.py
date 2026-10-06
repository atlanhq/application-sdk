#!/usr/bin/env python3
"""Build once on the bump PR, scan that image, and ship it at release (FND-3328).

The release used to build its image from scratch, so the image the PR scan saw
was never the image that shipped. Now:

1. On the bump-version PR, ``build-and-publish-app.yaml`` runs in candidate
   mode: the full release build (multi-arch, baked identity, GHCR base
   redirect) pushed as ``candidate-<tree>``, where ``<tree>`` is the git tree
   SHA of the commit built (``name``). The merge job pins that tag to its
   digest (``pin``), and the PR's vulnerability scan scans exactly that digest.
2. When the scan's Security Gate passes, it copies the scanned digest to
   ``scanned-<tree>`` (``mark``). Only a candidate that passed the gate carries
   that tag, so a bump PR merged past a red scan has nothing to promote.
3. At release, ``prepare`` looks up ``scanned-<tree>`` for the tree of the
   commit being released (``lookup``). Found: ``merge`` copies that manifest to
   every release tag (``promote``) and skips the build. Not found: the release
   rebuilds, and its scan blocks the publish.

Why the TREE and not the commit: the bump PR is squash-merged, so the commit
that gets tagged is never a commit the PR built. Its tree is identical to the
tree of the PR's merge ref whenever the base did not move in between, and the
tree is what the image is built from. When the base did move, the trees differ
and the release falls back to a rebuild, which is the safe direction.

Every copy here is a registry manifest GET + PUT of the same bytes, so the
promoted tags carry the scanned digest byte for byte; ``promote`` re-reads each
tag and fails if any digest differs.

What a promoted image reports about itself: ``app/atlan_build.json`` is baked
at candidate build time. ``app_version`` is ``v<pyproject version>``, the exact
tag tag-and-release.yaml cuts, so it matches what the publish job sends to the
Global Marketplace (``lookup`` refuses to promote when the release tag is
anything else). ``commit_sha`` / ``build_id`` / ``image`` name the bump PR's
merge-ref commit and the candidate tag: a commit whose tree equals the
release commit's, not the release commit itself.

Unit-tested in tests/test_release_candidate.py. Logic lives here, not in
workflow ``run:`` blocks, per docs/standards/ci.md.

Subcommands and their environment:

``name``     REPO; reads ``pyproject.toml`` and git in the working directory.
             Writes ``branch``, ``image_tag``, ``ghcr_image``,
             ``ghcr_branch_tag``, ``gm_version``.
``pin``      IMAGE (``ghcr.io/atlanhq/<repo>:candidate-<tree>``), GHCR_USER,
             GHCR_TOKEN. Writes ``image_ref`` (``<IMAGE>@<digest>``).
``mark``     IMAGE (a ``pin`` output), EXPECTED_REPO (the calling repository;
             IMAGE must name it), GHCR_USER, GHCR_TOKEN.
``lookup``   REPO, RELEASE_TAG, GHCR_USER, GHCR_TOKEN; reads git and
             ``pyproject.toml``. Writes ``promote_source``
             (``ghcr.io/atlanhq/<repo>@<digest>``) or nothing. Never fails the
             step: anything it cannot establish means "rebuild".
``promote``  SOURCE (a ``lookup`` output), TAGS (newline-separated full refs),
             GHCR_USER, GHCR_TOKEN.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resolve_base_redirect as rbr  # noqa: E402

GHCR_HOST = "ghcr.io"
GHCR_ORG = "atlanhq"
CANDIDATE_PREFIX = "candidate-"
SCANNED_PREFIX = "scanned-"

_TREE_RE = r"[0-9a-f]{40}"
_DIGEST_RE = r"sha256:[0-9a-f]{64}"
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A PEP 440 version's character set. Anything else (a newline above all) would
# be written into GITHUB_OUTPUT and the image tags, so it is refused here.
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+!_-]{0,63}$")
_CANDIDATE_RE = re.compile(
    rf"^{GHCR_HOST}/{GHCR_ORG}/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    rf":{CANDIDATE_PREFIX}(?P<tree>{_TREE_RE})$"
)
_PINNED_RE = re.compile(
    rf"^{GHCR_HOST}/{GHCR_ORG}/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    rf":{CANDIDATE_PREFIX}(?P<tree>{_TREE_RE})@(?P<digest>{_DIGEST_RE})$"
)
_SOURCE_RE = re.compile(
    rf"^{GHCR_HOST}/{GHCR_ORG}/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    rf"@(?P<digest>{_DIGEST_RE})$"
)
_TAG_REF_RE = re.compile(
    rf"^{GHCR_HOST}/{GHCR_ORG}/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$"
)

GitFn = Callable[[list[str]], str]


class CandidateError(Exception):
    """An input or registry answer this script refuses to act on."""


# ── Naming ────────────────────────────────────────────────────────────────────


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def head_tree(git: GitFn = _git) -> str:
    tree = git(["rev-parse", "HEAD^{tree}"])
    if not re.fullmatch(_TREE_RE, tree):
        raise CandidateError(f"unexpected tree SHA from git: {tree!r}")
    return tree


def pyproject_version(path: Path = Path("pyproject.toml")) -> str:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CandidateError(f"cannot read {path}: {exc}") from exc
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version.strip()):
        raise CandidateError(f"{path} has no valid [project].version")
    return version.strip()


def release_tag_for(version: str) -> str:
    """The tag tag-and-release.yaml cuts for *version*: ``v<version>``."""
    return f"v{version}"


def ghcr_base(repo: str) -> str:
    if not _REPO_NAME_RE.fullmatch(repo):
        raise CandidateError(f"invalid repository name: {repo!r}")
    return f"{GHCR_HOST}/{GHCR_ORG}/{repo}"


def name_outputs(repo: str, tree: str, version: str) -> dict[str, str]:
    """Outputs that override ``prepare``'s tag step in candidate mode.

    ``branch`` + ``image_tag`` compose every tag the build and merge jobs push
    (``<branch>-<image_tag>-<arch>`` and ``<branch>-<image_tag>``), so setting
    them is enough to land the whole build under ``candidate-<tree>``. The
    mutable branch tag is the same immutable ref: a candidate moves nothing.
    """
    base = ghcr_base(repo)
    image = f"{base}:{CANDIDATE_PREFIX}{tree}"
    return {
        "branch": CANDIDATE_PREFIX.rstrip("-"),
        "image_tag": tree,
        "ghcr_image": image,
        "ghcr_branch_tag": image,
        "gm_version": release_tag_for(version),
    }


# ── Registry ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Manifest:
    body: bytes
    media_type: str

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.body).hexdigest()


class Registry:
    """Minimal GHCR v2 client: read a manifest, write it under another tag.

    Auth is the standard bearer dance, reusing resolve_base_redirect's realm
    checks (HTTPS only, credentials only to the registry's own host). The
    token is requested with pull+push scope for the one repository.
    """

    def __init__(self, repo: str, user: str, token: str) -> None:
        self.base = ghcr_base(repo)
        self.path = self.base.partition("/")[2]
        self.user = user or "x-access-token"
        self.token = token
        self._bearer: Optional[str] = None

    def _url(self, reference: str) -> str:
        return (
            f"https://{GHCR_HOST}/v2/{self.path}/manifests/"
            f"{urllib.parse.quote(reference, safe=':')}"
        )

    def _authenticate(self, exc: urllib.error.HTTPError) -> None:
        challenge = rbr._parse_challenge(exc.headers.get("WWW-Authenticate", ""))
        if not challenge:
            raise CandidateError("registry answered 401 without a bearer challenge")
        challenge["scope"] = f"repository:{self.path}:pull,push"
        bearer = rbr._bearer_token(
            challenge, self.user, self.token, expected_host=GHCR_HOST
        )
        if not bearer:
            raise CandidateError("registry issued no token")
        self._bearer = bearer

    def _send(self, request: urllib.request.Request) -> tuple[bytes, str]:
        for attempt in (1, 2):
            if self._bearer:
                request.add_header("Authorization", f"Bearer {self._bearer}")
            try:
                with rbr._urlopen(request, timeout=rbr._HTTP_TIMEOUT_S) as response:  # type: ignore[operator]
                    return response.read(), response.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    self._authenticate(exc)
                    continue
                raise
        raise CandidateError("unreachable")  # pragma: no cover

    def get(self, reference: str) -> Optional[Manifest]:
        """Return the manifest at *reference* (tag or digest), or None on 404."""
        request = urllib.request.Request(self._url(reference), method="GET")
        request.add_header("Accept", rbr._ACCEPT)
        try:
            body, media_type = self._send(request)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return Manifest(body=body, media_type=media_type.split(";")[0].strip())

    def put(self, tag: str, manifest: Manifest) -> None:
        request = urllib.request.Request(
            self._url(tag), data=manifest.body, method="PUT"
        )
        request.add_header("Content-Type", manifest.media_type)
        self._send(request)


RegistryFactory = Callable[[str, str, str], Registry]


def _require(manifest: Optional[Manifest], what: str) -> Manifest:
    if manifest is None:
        raise CandidateError(f"{what} is not in the registry")
    return manifest


def copy_to_tag(registry: Registry, source: str, digest: str, tag: str) -> None:
    """Write the manifest at *digest* under *tag*, and prove the tag now holds it."""
    manifest = _require(registry.get(digest), f"{source}@{digest}")
    if manifest.digest != digest:
        raise CandidateError(
            f"registry served {manifest.digest} when asked for {digest}"
        )
    registry.put(tag, manifest)
    written = _require(registry.get(tag), f"{registry.base}:{tag}")
    if written.digest != digest:
        raise CandidateError(
            f"{registry.base}:{tag} reads back as {written.digest}, expected {digest}"
        )


# ── Subcommands ───────────────────────────────────────────────────────────────


def cmd_name(env: dict[str, str], git: GitFn = _git) -> dict[str, str]:
    return name_outputs(
        env.get("REPO", ""),
        head_tree(git),
        pyproject_version(Path(env.get("PYPROJECT", "pyproject.toml"))),
    )


def cmd_pin(env: dict[str, str], factory: RegistryFactory = Registry) -> dict[str, str]:
    image = env.get("IMAGE", "")
    match = _CANDIDATE_RE.fullmatch(image)
    if not match:
        raise CandidateError(f"not a candidate image ref: {image!r}")
    registry = factory(
        match["repo"], env.get("GHCR_USER", ""), env.get("GHCR_TOKEN", "")
    )
    manifest = _require(registry.get(f"{CANDIDATE_PREFIX}{match['tree']}"), image)
    return {"image_ref": f"{image}@{manifest.digest}"}


def cmd_mark(
    env: dict[str, str], factory: RegistryFactory = Registry
) -> dict[str, str]:
    image = env.get("IMAGE", "")
    match = _PINNED_RE.fullmatch(image)
    if not match:
        raise CandidateError(f"not a pinned candidate image ref: {image!r}")
    expected = env.get("EXPECTED_REPO", "")
    if not expected or match["repo"] != expected:
        raise CandidateError(
            f"refusing to mark {image!r}: not an image of the calling "
            f"repository {expected!r}"
        )
    registry = factory(
        match["repo"], env.get("GHCR_USER", ""), env.get("GHCR_TOKEN", "")
    )
    tag = f"{SCANNED_PREFIX}{match['tree']}"
    copy_to_tag(registry, image, match["digest"], tag)
    print(f"Marked {registry.base}@{match['digest']} as :{tag}", flush=True)
    return {}


def cmd_lookup(
    env: dict[str, str],
    factory: RegistryFactory = Registry,
    git: GitFn = _git,
) -> dict[str, str]:
    """Find the scanned candidate for this release, or say why there is none.

    Fails open towards a rebuild: every problem is printed and returns no
    ``promote_source``, never an exception.
    """
    repo = env.get("REPO", "")
    release_tag = env.get("RELEASE_TAG", "").strip()
    try:
        tree = head_tree(git)
        expected = release_tag_for(
            pyproject_version(Path(env.get("PYPROJECT", "pyproject.toml")))
        )
        if release_tag != expected:
            print(
                f"::notice::Release tag {release_tag!r} is not {expected!r}, the "
                "version a bump-PR candidate bakes in; rebuilding.",
                flush=True,
            )
            return {}
        registry = factory(repo, env.get("GHCR_USER", ""), env.get("GHCR_TOKEN", ""))
        tag = f"{SCANNED_PREFIX}{tree}"
        manifest = registry.get(tag)
    except (
        CandidateError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
        OSError,
        ValueError,
    ) as exc:
        print(f"::warning::Release candidate lookup failed ({exc}); rebuilding.")
        return {}
    if manifest is None:
        print(
            f"::notice::No scanned candidate for tree {tree} (bump PR not scanned "
            "on this exact tree, e.g. the base moved before merge); rebuilding, "
            "and the release scan blocks the publish.",
            flush=True,
        )
        return {}
    source = f"{registry.base}@{manifest.digest}"
    print(f"Promoting scanned candidate {source} (:{tag})", flush=True)
    return {"promote_source": source}


def cmd_promote(
    env: dict[str, str], factory: RegistryFactory = Registry
) -> dict[str, str]:
    source = env.get("SOURCE", "")
    match = _SOURCE_RE.fullmatch(source)
    if not match:
        raise CandidateError(f"not a digest ref: {source!r}")
    tags: list[str] = []
    for ref in (line.strip() for line in env.get("TAGS", "").splitlines()):
        if not ref:
            continue
        tag_match = _TAG_REF_RE.fullmatch(ref)
        if not tag_match or tag_match["repo"] != match["repo"]:
            raise CandidateError(
                f"refusing to promote to {ref!r}: not a tag of {match['repo']}"
            )
        if tag_match["tag"] not in tags:
            tags.append(tag_match["tag"])
    if not tags:
        raise CandidateError("no tags to promote to")
    registry = factory(
        match["repo"], env.get("GHCR_USER", ""), env.get("GHCR_TOKEN", "")
    )
    for tag in tags:
        copy_to_tag(registry, source, match["digest"], tag)
        print(f"  {registry.base}:{tag} -> {match['digest']}", flush=True)
    return {"digest": match["digest"]}


COMMANDS = {
    "name": cmd_name,
    "pin": cmd_pin,
    "mark": cmd_mark,
    "lookup": cmd_lookup,
    "promote": cmd_promote,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: {argv[0]} {{{','.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    try:
        outputs = COMMANDS[argv[1]](dict(os.environ))
    except (
        CandidateError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as exc:
        print(f"::error::release_candidate {argv[1]}: {exc}", flush=True)
        return 1
    for key, value in outputs.items():
        if "\n" in value or "\r" in value:
            print(f"::error::release_candidate {argv[1]}: {key} has a line break")
            return 1
    path = os.environ.get("GITHUB_OUTPUT")
    if path and outputs:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")
    for key, value in outputs.items():
        print(f"{key}={value}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
