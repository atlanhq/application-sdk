#!/usr/bin/env python3
"""Conventional-commit PR title validator for the connector fleet.

Backs the reusable `.github/workflows/commits.yaml`, which every
bootstrap-managed connector repo calls from its own `commits.yaml` shim.

Unlike this repo's own `pr_title_convention.py` — which decides *which*
conventional type a PR must use from the SDK's own directory layout
(`application_sdk/`, `contract-toolkit/`, `packages/conformance/`, …) — this
one is deliberately path-agnostic: it only asks whether the title is a
well-formed conventional commit at all. It runs in ~54 repos with ~54
different layouts, so any path rule here would be wrong somewhere.

Why the fleet needs it: connector repos release through
`release-version-bump.yaml`, whose `release.py` derives the semver bump from
`^feat[(!:]` / `^fix[(!:]` / `BREAKING CHANGE` in the squash-merge subjects,
and whose `update_changelog.py` files each subject into Features/Fixes/Chores
by the same grammar. Those repos squash-merge, so the subject *is* the PR
title. A title outside the grammar silently lands in neither the version
calculation nor a changelog section.

Rules:

  0. Automation titles are ignored entirely — the version-bump/release PRs
     opened by `release-version-bump.yaml` (branch `bump-version-*`, or a
     `Bump version to …` / `chore(…): release …` title), and PRs opened by an
     exempt actor (Dependabot by default: its titles are machine-generated and
     no human on the PR can make them conventional). Renovate is deliberately
     NOT exempt — the fleet preset forces `semanticCommitType: chore`, so its
     titles are conventional already and a regression there is worth catching.

  1. Everything else must match `type(optional-scope)!: description`, with
     `type` drawn from the configured list (the types documented in
     docs/standards/commits.md by default).

Only the grammar is enforced, never prose style (capitalisation, trailing
period, imperative mood). Those are review comments, not a red check across
the fleet — and nothing downstream parses them.

Usage:
    python3 conventional_pr_title.py \
        --pr-title "<title>" \
        --head-ref "<branch>" \
        --actor "<pr author login>" \
        --types "feat,fix,…" \
        --exempt-actors "dependabot[bot]" \
        --comment-out <path to write the sticky-comment markdown on violation>

Writes `violation` and `error_message` to $GITHUB_OUTPUT (or prints
`key=value` lines if $GITHUB_OUTPUT is unset). Always exits 0 — the calling
workflow gates on the `violation` output in a separate step.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# The conventional-commit types documented in docs/standards/commits.md. The
# workflow surfaces this as an input, so a repo with an extra type (or a
# deliberately narrower set) configures it rather than forking the script.
DEFAULT_TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

# Actors whose PR titles this guard never polices. See rule 0 above.
DEFAULT_EXEMPT_ACTORS: tuple[str, ...] = ("dependabot[bot]",)

RELEASE_RE = re.compile(r"^chore(\([^)]*\))?: release ")
BUMP_RE = re.compile(r"^Bump version to ")

# One permissive shape match, so the *reason* a title fails can be specific
# (unknown type / empty scope / no description) instead of one blanket
# "doesn't match the pattern". Type is captured loosely (any word chars) and
# checked against the allowed list separately for exactly that reason.
SHAPE_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)(?P<scope>\([^()]*\))?(?P<breaking>!)?:(?P<desc>.*)$"
)


def is_exempt(
    pr_title: str, head_ref: str, actor: str, exempt_actors: tuple[str, ...]
) -> str:
    """Return a human-readable exemption reason, or "" if the title is policed."""
    if actor and actor in exempt_actors:
        return f"PR opened by exempt actor {actor!r}"
    if head_ref.startswith("bump-version"):
        return "version-bump branch"
    if RELEASE_RE.match(pr_title) or BUMP_RE.match(pr_title):
        return "version-bump / release automation title"
    return ""


def validate(pr_title: str, types: tuple[str, ...]) -> str:
    """Return an error message for *pr_title*, or "" if it is well-formed."""
    title = pr_title.strip()
    allowed = ", ".join(f"'{t}'" for t in types)

    m = SHAPE_RE.match(title)
    if not m:
        return (
            "PR title is not a conventional commit. Expected "
            "'<type>[optional scope][!]: <description>' "
            f"(for example 'fix(sql): handle a closed cursor'). Allowed types: {allowed}."
        )

    kind = m.group("type")
    if kind not in types:
        # A capitalised type is the common near-miss; name it rather than
        # leaving the author to diff their title against the list by eye.
        hint = (
            f" Types are lower-case — did you mean '{kind.lower()}'?"
            if kind.lower() in types
            else ""
        )
        return (
            f"'{kind}' is not an allowed commit type. Allowed types: {allowed}.{hint}"
        )

    scope = m.group("scope")
    if scope is not None and not scope[1:-1].strip():
        return (
            "PR title has an empty scope. Either name the scope "
            f"(e.g. '{kind}(auth): …') or drop the parentheses ('{kind}: …')."
        )

    if not m.group("desc").strip():
        return f"PR title has no description after '{kind}:'."

    if not m.group("desc").startswith(" "):
        return (
            "PR title needs a space after the colon "
            f"('{kind}: description', not '{kind}:description')."
        )

    return ""


def comment_body(error_message: str, types: tuple[str, ...]) -> str:
    """Return the sticky-comment markdown shown on a violation."""
    type_lines = "\n".join(f"- `{t}`" for t in types)
    return f"""\
### \U0001f3f7️ This PR title is not a conventional commit

{error_message}

**Format:** `<type>[optional scope][!]: <description>`

_Examples:_ `feat: add incremental extraction`, `fix(sql): retry a closed
cursor`, `feat(api)!: drop the v1 payload shape`

**Allowed types:**
{type_lines}

This repo releases from its squash-merge subjects — which are these PR titles
— so a title outside the grammar lands in neither the version bump nor the
changelog.

Editing the PR title re-runs this check and clears this comment automatically.
"""


def parse_list(value: str) -> tuple[str, ...]:
    """Split a comma- and/or whitespace-separated flag value into entries."""
    return tuple(item for item in re.split(r"[,\s]+", value.strip()) if item)


def _write_output(key: str, value: str) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}"
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(line + "\n")
    else:
        print(line)


def run(
    pr_title: str,
    head_ref: str,
    actor: str,
    types: tuple[str, ...],
    exempt_actors: tuple[str, ...],
    comment_out_path: str,
) -> str:
    """Core decision logic, returned as the error message ("" when valid)."""
    exempt = is_exempt(pr_title, head_ref, actor, exempt_actors)
    if exempt:
        print(f"Ignored by this guard: {exempt}.")
        return ""

    error_message = validate(pr_title, types)
    if not error_message:
        print("PR title is a well-formed conventional commit. ✅")
        return ""

    print(f"Violation: {error_message}")
    with open(comment_out_path, "w") as fh:
        fh.write(comment_body(error_message, types))
    return error_message


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument(
        "--types",
        default=",".join(DEFAULT_TYPES),
        help="Comma-separated allowed conventional-commit types",
    )
    parser.add_argument(
        "--exempt-actors",
        default=",".join(DEFAULT_EXEMPT_ACTORS),
        help="Comma-separated PR-author logins this guard never polices",
    )
    parser.add_argument(
        "--comment-out",
        required=True,
        help="Path to write the sticky-comment markdown when the title is invalid",
    )
    args = parser.parse_args()

    types = parse_list(args.types) or DEFAULT_TYPES
    exempt_actors = parse_list(args.exempt_actors)

    print(f"PR title: {args.pr_title}")
    error_message = run(
        args.pr_title,
        args.head_ref,
        args.actor,
        types,
        exempt_actors,
        args.comment_out,
    )

    _write_output("violation", "true" if error_message else "false")
    _write_output("error_message", error_message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
