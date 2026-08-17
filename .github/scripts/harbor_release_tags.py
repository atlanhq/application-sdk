#!/usr/bin/env python3
"""Compute the ``app-runtime-base`` tag ladder for a Harbor/GHCR release.

Lifted verbatim out of the inline shell in
``.github/workflows/harbor-release.yaml``: the tag-prefix branch and the
event-dependent ladder are both `if`/`else` chains, which
``docs/standards/ci.md`` keeps out of workflow ``run:`` blocks so they can be
regression-tested. Behaviour is unchanged from that shell.

Two decisions live here.

**The prefix** labels non-release builds:

* ``release`` event -> always ``main``. Release image tags are the version
  ladder below, never prefix-scoped, so the prefix is only a label.
* explicit ``--tag-prefix-input`` -> used as given, after a charset check. A
  prefix reaches a registry reference, so anything outside
  ``[A-Za-z0-9._-]`` is rejected rather than sanitised: silently rewriting an
  operator's input would publish a tag they did not ask for.
* otherwise -> the branch name, with every other character collapsed to ``-``.

**The ladder** is published to both registries (identical manifest, see
``docs/standards/build-security.md``):

* stable release -> ``:latest``, ``:X.Y.Z``, ``:X.Y``, ``:X``, ``:sha-<sha>``.
* pre-release (any version containing ``-``, e.g. ``3.1.0-rc1``) -> ``:X.Y.Z``
  and ``:sha-<sha>`` only. A pre-release must never advance ``:latest`` or the
  floating major/minor aliases, which tenants resolve.
* ``workflow_dispatch`` -> ``:<prefix>-latest``, ``:<prefix>-<version>``,
  ``:sha-<sha>``.

Writes ``tag_prefix``, ``version``, ``sha`` and the newline-delimited ``tags``
to ``$GITHUB_OUTPUT`` (or stdout when unset).
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys

#: Registries that receive the identical manifest for every tag below.
REPOS = (
    "registry.atlan.com/public/app-runtime-base",
    "ghcr.io/atlanhq/app-runtime-base",
)

#: Characters legal in a tag prefix. Matches the grep in the shell this replaces.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Length of the short SHA used in `:sha-<sha>`.
_SHORT_SHA = 7


class PrefixError(ValueError):
    """An explicitly supplied ``--tag-prefix`` is not registry-safe."""


def sanitize_branch(ref_name: str) -> str:
    """Collapse every run of characters illegal in a tag into a single ``-``.

    Mirrors ``tr -cs 'A-Za-z0-9._-' '-'``: `-s` squeezes repeats, so
    ``feat/my branch`` becomes ``feat-my-branch``, not ``feat-my-branch``
    with a doubled separator.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ref_name)


def resolve_prefix(event_name: str, tag_prefix_input: str, ref_name: str) -> str:
    """Return the tag prefix for this run.

    Raises:
        PrefixError: ``tag_prefix_input`` contains characters that are not
            legal in an image tag.
    """
    if event_name == "release":
        return "main"
    if tag_prefix_input:
        if not _PREFIX_RE.match(tag_prefix_input):
            raise PrefixError(
                f"Invalid tag_prefix {tag_prefix_input!r}: must match ^[A-Za-z0-9._-]+$"
            )
        return tag_prefix_input
    return sanitize_branch(ref_name)


def is_prerelease(version: str) -> bool:
    """A version carrying a pre-release segment, i.e. containing ``-``.

    Matches the shell's ``grep -qvF '-'`` test rather than parsing semver:
    the ladder only needs "does this advance the floating aliases or not".
    """
    return "-" in version


def tag_suffixes(event_name: str, version: str, prefix: str, short_sha: str) -> list:
    """Return the tag suffixes (the part after ``<repo>:``) for this run."""
    sha_tag = f"sha-{short_sha}"
    if event_name != "release":
        return [f"{prefix}-latest", f"{prefix}-{version}", sha_tag]
    if is_prerelease(version):
        return [version, sha_tag]
    major = version.split(".")[0]
    minor = ".".join(version.split(".")[:2])
    return ["latest", version, minor, major, sha_tag]


def build_tags(event_name: str, version: str, prefix: str, short_sha: str) -> list:
    """Return every fully-qualified tag, one repo's ladder after the other."""
    suffixes = tag_suffixes(event_name, version, prefix, short_sha)
    return [f"{repo}:{suffix}" for repo in REPOS for suffix in suffixes]


def read_version(pyproject_path: str) -> str:
    """Return the ``version = "..."`` value from a pyproject's top level.

    Reads the first top-level ``version =`` assignment, which is what the
    ``awk -F'"' '/^version = /'`` in the shell did — anchored at column 0, so
    a ``version`` key nested inside a table is not matched.
    """
    with open(pyproject_path, encoding="utf-8") as fh:
        for line in fh:
            match = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if match:
                return match.group(1)
    raise ValueError(f'no top-level `version = "..."` found in {pyproject_path}')


def _write_outputs(pairs: dict) -> None:
    """Emit ``key=value`` pairs to $GITHUB_OUTPUT, heredoc-quoting multi-line ones."""
    target = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for key, value in pairs.items():
        if "\n" in value:
            delim = f"EOF_{secrets.token_hex(8)}"
            lines.append(f"{key}<<{delim}\n{value}\n{delim}")
        else:
            lines.append(f"{key}={value}")
    body = "\n".join(lines) + "\n"
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(body)
    else:
        sys.stdout.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--sha", required=True, help="Full commit SHA; truncated here.")
    parser.add_argument("--tag-prefix", default="", help="Explicit prefix, if any.")
    parser.add_argument("--pyproject", default="pyproject.toml")
    args = parser.parse_args()

    try:
        prefix = resolve_prefix(args.event_name, args.tag_prefix, args.ref_name)
    except PrefixError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    version = read_version(args.pyproject)
    short_sha = args.sha[:_SHORT_SHA]
    tags = build_tags(args.event_name, version, prefix, short_sha)

    print(f"Event: {args.event_name}  version: {version}  prefix: {prefix}")
    print("Tags:")
    for tag in tags:
        print(f"  {tag}")

    _write_outputs(
        {
            "tag_prefix": prefix,
            "version": version,
            "sha": short_sha,
            "tags": "\n".join(tags),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
