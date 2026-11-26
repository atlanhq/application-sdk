"""Pull requests the reviewer does not review, decided in code.

Some pull requests have nothing for a reviewer to judge: a dependency bot's
bump, a resync of the files the SDK bootstrap owns, a release train's version
bump, a change to prose. Asking a model about them costs money and produces
noise. `classify` names the class from facts GitHub reports (who opened the
pull request, its branch, who wrote each commit, which files changed) and the
lists in `config.toml`. No model is involved, and nothing here reads the
content of a file.

On `/review` for an exempt pull request the summary says `Exempt: <class>` and
why, and the status is `success` with `exempt: <class>`. `/review force`
reviews it anyway, and a pull request that has been reviewed once is never
exempt again (review.run): a forced review that found something must not be
papered over by the next plain `/review`.

Every other pull request a bot opens is reviewed like a person's.

lens has no such notion: every pull request it is asked about is reviewed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .advisory import glob_match

DEPENDENCY = "dependency update"
RESYNC = "conformance resync"
VERSION_BUMP = "version bump"
DOCS = "docs only"

DEPENDENCY_BOTS = ("atlan-app-fleet[bot]", "renovate[bot]", "dependabot[bot]")
DEPENDENCY_BRANCHES = ("renovate/", "dependabot/")
# The vulnerability agent's dependency fixes (FND-5321). The same App also
# opens code pull requests on other branches, so it is exempt only on these
# branches, only when it wrote every commit, and only when every changed
# file is a dependency manifest or lock: a vuln branch carrying code is
# reviewed.
VULN_BOTS = ("connectivity-ai[bot]",)
VULN_BRANCHES = ("vuln-agent/",)
DEPENDENCY_FILES = (
    "uv.lock",
    "pyproject.toml",
    "contract/PklProject",
    "contract/PklProject.deps.json",
    "contract_schema.lock.json",
)
RESYNC_BOTS = ("atlan-conformance-sync[bot]",)
VERSION_BUMP_BRANCHES = ("bump-version",)
# What `atlan-application-sdk-conformance bootstrap` writes, removes or merges
# into in a connector repo (packages/conformance/conformance/bootstrap:
# render.py for the lists, command.py for the single files). A workflow is
# named one by one: a glob over `.github/workflows/` would also cover the
# app's own. A test reads the lists out of render.py and fails when a
# destination is not covered.
BOOTSTRAP_MANAGED = (
    ".github/workflows/conformance.yaml",
    ".github/workflows/conformance-upload-sarif.yaml",
    ".github/workflows/checks.yml",
    ".github/workflows/commits.yaml",
    ".github/workflows/release-gate.yaml",
    ".github/workflows/connector-review-gate.yaml",
    ".github/workflows/release.yaml",
    ".github/workflows/tag-and-publish.yaml",
    ".github/workflows/vulnerability-scan.yml",
    ".github/workflows/build-and-publish.yaml",
    ".github/workflows/stale.yml",
    ".github/workflows/generated-freshness.yaml",
    ".github/workflows/test-commands.yaml",
    ".github/workflows/tests.yaml",
    # The connector-reviewer shim: a resync that only updates it is still a resync.
    ".github/workflows/connector-reviewer.yaml",
    # Shims bootstrap once wrote and now removes.
    ".github/workflows/docstring-coverage.yaml",
    ".github/workflows/auto-fix.yml",
    ".github/workflows/update-dashboard.yml",
    ".github/workflows/renovate-auto-approve.yml",
    ".github/workflows/porter-bundle-cnab.yaml",
    # Never bootstrap-managed: bootstrap never wrote it and only removes it (a per-repo
    # caller of atlanhq/.github's reusable-dep-cooldown.yml; see render.py).
    ".github/workflows/dep-cooldown.yml",
    # Never bootstrap-managed: hand-copied callers of marketplace-releases' dev-tenant
    # Playwright workflows; bootstrap only removes them (see render.py).
    ".github/workflows/e2e-pr-init-check.yaml",
    ".github/workflows/e2e-tests-feature.yaml",
    # Never bootstrap-managed: a hand-written weekly uv-upgrade job Renovate replaces.
    ".github/workflows/update-deps.yml",
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
    ".github/actions/run-conformance-detect/**",
    ".github/scripts/build_conformance_args.py",
    ".github/scripts/probe_code_scanning.py",
    ".github/scripts/connector_review_gate.py",
    ".github/scripts/release_gate.py",
    ".github/ci-system-deps.txt",
    ".github/connector-reviewer/principles/README.md",
    ".claude/**",
    "scripts/fetch-review-rules.sh",
    "scripts/write-connector-review-marker.sh",
    ".mothership/**",
    "CLAUDE.md",
    "AGENTS.md",
    "renovate.json",
    "contract_schema.lock.json",
    ".gitignore",
)
# Prose that steers the reviewer or a coding agent is not "docs".
_DOCS_GUARDED_DIRS = (".github/connector-reviewer/", ".claude/")
_DOCS_GUARDED_NAMES = ("CLAUDE.md", "AGENTS.md")


@dataclass
class ExemptConfig:
    """The `[exempt]` table of config.toml. Every list can change without code."""

    dependency_bots: list[str] = field(default_factory=lambda: list(DEPENDENCY_BOTS))
    dependency_branches: list[str] = field(
        default_factory=lambda: list(DEPENDENCY_BRANCHES)
    )
    vuln_bots: list[str] = field(default_factory=lambda: list(VULN_BOTS))
    vuln_branches: list[str] = field(default_factory=lambda: list(VULN_BRANCHES))
    dependency_files: list[str] = field(default_factory=lambda: list(DEPENDENCY_FILES))
    resync_bots: list[str] = field(default_factory=lambda: list(RESYNC_BOTS))
    bootstrap_managed: list[str] = field(
        default_factory=lambda: list(BOOTSTRAP_MANAGED)
    )
    version_bump_branches: list[str] = field(
        default_factory=lambda: list(VERSION_BUMP_BRANCHES)
    )


@dataclass(frozen=True)
class Exemption:
    kind: str  # one of DEPENDENCY, RESYNC, VERSION_BUMP, DOCS
    why: str  # one sentence for the summary
    reminder: str = ""  # what the pull request still needs, if anything


def is_managed(path: str, cfg: ExemptConfig) -> bool:
    """Whether the SDK bootstrap owns `path` in a connector repo."""
    return any(glob_match(path, g) for g in cfg.bootstrap_managed)


def _docs(path: str) -> bool:
    if path.startswith(_DOCS_GUARDED_DIRS):
        return False
    if PurePosixPath(path).name in _DOCS_GUARDED_NAMES:
        return False
    return path.endswith(".md") or path.startswith("docs/")


def classify(
    *,
    author: str,
    head_ref: str,
    commit_authors: Sequence[str],
    paths: Sequence[str],
    cfg: ExemptConfig,
) -> Exemption | None:
    """The class this pull request is exempt under, or None when it is reviewed.

    `commit_authors` holds the GitHub login of every commit's author, "" for a
    commit GitHub could not attribute; an empty sequence means they are not
    known. `paths` is every path the pull request's diff touches, a rename's
    old path included. Unknown is never exempt: a class that needs the commit
    authors or the paths does not hold without them."""
    by = set(commit_authors)
    if (
        author in cfg.dependency_bots
        and head_ref.startswith(tuple(cfg.dependency_branches))
        and by
        and by <= set(cfg.dependency_bots)
    ):
        return Exemption(
            DEPENDENCY,
            f"opened by {author} on `{head_ref}`, and every commit is a dependency bot's",
        )
    if (
        author in cfg.vuln_bots
        and head_ref.startswith(tuple(cfg.vuln_branches))
        and by
        and by <= set(cfg.vuln_bots)
        and paths
        and all(any(glob_match(p, g) for g in cfg.dependency_files) for p in paths)
    ):
        return Exemption(
            DEPENDENCY,
            f"opened by {author} on `{head_ref}`, every commit is its own, and all "
            f"{len(paths)} changed file(s) are dependency manifests or locks",
        )
    if (
        author in cfg.resync_bots
        and by
        and by <= set(cfg.resync_bots)
        and paths
        and all(is_managed(p, cfg) for p in paths)
    ):
        return Exemption(
            RESYNC,
            f"opened by {author}, every commit is the resync bot's, and all "
            f"{len(paths)} changed file(s) are managed by the SDK bootstrap",
        )
    if head_ref.startswith(tuple(cfg.version_bump_branches)):
        return Exemption(
            VERSION_BUMP,
            f"the branch `{head_ref}` is a version bump",
            reminder="It still needs `/gate-check` before it can merge.",
        )
    if paths and all(_docs(p) for p in paths):
        return Exemption(
            DOCS,
            f"all {len(paths)} changed file(s) are Markdown or under `docs/`, and "
            "none is a principle or an agent instruction file",
        )
    return None
