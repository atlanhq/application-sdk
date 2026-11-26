"""
release_train.py
----------------
The release train for the packages in this repo that version themselves from
git tags: ``atlan-application-sdk-conformance`` and ``app-contract-toolkit``.

Each package is a :class:`Train` — a package root, a tag prefix, a version file
and a changelog. One code path computes the next semver from the conventional
commits since the last release tag, rewrites the version, and prepends the
changelog block. Before FND-4672 each package had its own 300-line copy of this
script; the copies were 81% identical, carried their own "breaking change"
regex, and that regex disagreed with the one ``release.py`` uses for the SDK.
"Breaking" is now decided by :mod:`conventional_breaking` for every package.

Usage:
    python release_train.py bump <train>          # release workflow
    python release_train.py publish-meta <train>  # publish workflow

``bump`` writes ``skip=true`` to GITHUB_OUTPUT when there is nothing to release
(no unreleased commits, the merge did not land on main, or main already carries
the computed version) and otherwise ``skip=false`` plus ``old``, ``new`` and
``tag``. ``publish-meta`` writes ``version``, ``tag`` and ``merge_sha`` for the
publish workflow, refusing a tag that is not ``<prefix>X.Y.Z``.

Environment:
    GITHUB_OUTPUT      - path to the GitHub Actions output file (optional for
                         local runs; falls back to printing to stdout)
    GITHUB_REPOSITORY  - owner/repo (defaults to atlanhq/application-sdk)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple

import release_guard
from _gha import set_output
from conventional_breaking import declares_breaking_change


@dataclass(frozen=True)
class Train:
    """One independently released package."""

    #: CLI name, also the conventional-commit scope and the bump-branch suffix.
    name: str
    #: Release tags are ``<tag_prefix>X.Y.Z``.
    tag_prefix: str
    #: The single file that declares the version. Python packages read their
    #: ``__version__`` back from installed metadata, so there is no second copy.
    version_file: str
    #: ``git log`` pathspec for the commits that belong to this package.
    pathspec: tuple[str, ...]
    changelog: str
    release_notes_file: str

    def release_tag(self, version: str) -> str:
        return f"{self.tag_prefix}{version}"

    def is_release_tag(self, tag: str) -> bool:
        """True for ``<prefix>X.Y.Z`` exactly.

        Anything else under the prefix — a personal pre-release such as
        ``contract-toolkit-v0.11.0-someone.1`` — is not a release of this
        train: it is never a changelog base and never counts as "a release
        exists".
        """
        rest = tag.removeprefix(self.tag_prefix)
        return rest != tag and _SEMVER.fullmatch(rest) is not None


_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")

# Lock files under the conformance package are regenerated wholesale by
# Renovate's lockFileMaintenance and are not shipped in the wheel, so a commit
# that touches nothing else is pure changelog noise — and on its own it was
# enough to trigger a version bump. Excluding them by path rather than by commit
# subject keeps mixed commits intact: a PR that changes a rule *and* refreshes
# uv.lock still appears. Real dependency changes move pyproject.toml, which
# stays in scope. The `glob` magic makes `**/` match zero or more directories.
_CONFORMANCE_DIR = "packages/conformance"
_LOCK_FILES = ("uv.lock",)

CONFORMANCE = Train(
    name="conformance",
    tag_prefix="conformance-v",
    version_file=f"{_CONFORMANCE_DIR}/pyproject.toml",
    pathspec=(
        f"{_CONFORMANCE_DIR}/**",
        # The SDK ledger the package ships.
        "contract_schema.lock.json",
        *(f":(exclude,glob){_CONFORMANCE_DIR}/**/{name}" for name in _LOCK_FILES),
    ),
    changelog=f"{_CONFORMANCE_DIR}/CHANGELOG.md",
    release_notes_file="/tmp/conformance-release-notes.md",
)

CONTRACT_TOOLKIT = Train(
    name="contract-toolkit",
    tag_prefix="contract-toolkit-v",
    version_file="contract-toolkit/src/PklProject",
    pathspec=("contract-toolkit/**",),
    changelog="contract-toolkit/CHANGELOG.md",
    release_notes_file="/tmp/contract-toolkit-release-notes.md",
)

TRAINS: dict[str, Train] = {t.name: t for t in (CONFORMANCE, CONTRACT_TOOLKIT)}

# The first `version = "..."` in the file: the [project] version in a
# pyproject.toml, the package block's version in a PklProject.
_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)


class Commit(NamedTuple):
    sha: str  # 7 characters
    subject: str
    body: str


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def tag_exists(tag: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
            capture_output=True,
        ).returncode
        == 0
    )


def release_tags(train: Train) -> list[str]:
    listed = _run(["git", "tag", "--list", f"{train.tag_prefix}*"])
    return [t for t in listed.splitlines() if train.is_release_tag(t.strip())]


def get_commits(train: Train, base: str) -> list[Commit]:
    """Commits in ``base..HEAD`` touching the train's pathspec, newest first."""
    raw = _run(
        [
            "git",
            "log",
            f"{base}..HEAD",
            "--format=%H%x00%s%x00%b%x1e",  # NUL-delimited fields, RS records
            "--",
            *train.pathspec,
        ]
    )
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x00", 2)
        if len(parts) < 2:
            continue
        body = parts[2].strip() if len(parts) > 2 else ""
        commits.append(Commit(parts[0].strip()[:7], parts[1].strip(), body))
    return commits


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def read_version(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        m = _VERSION_RE.search(f.read())
    if not m:
        sys.exit(f"ERROR: could not parse version from {path}")
    return m.group(1)


def write_version(path: str, current: str, new: str) -> None:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    old_str = f'version = "{current}"'
    if old_str not in text:
        sys.exit(
            f"ERROR: could not find {old_str!r} in {path} — "
            "version string format may have changed."
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old_str, f'version = "{new}"', 1))


def _is_breaking(commit: Commit) -> bool:
    return declares_breaking_change(f"{commit.subject}\n{commit.body}")


def compute_bump(commits: Sequence[Commit]) -> str:
    if any(_is_breaking(c) for c in commits):
        return "major"
    if any(re.match(r"feat[(!:]", c.subject) for c in commits):
        return "minor"
    return "patch"


def bump_version(current: str, bump: str) -> str:
    major, minor, patch = (int(x) for x in current.split("."))
    if bump == "major":
        # Semver §4: while the major version is zero the public API is not
        # declared stable, so a breaking change bumps the MINOR, not the major.
        # Cutting 1.0.0 is a deliberate stability declaration about the whole
        # package and must stay a human decision — never a side effect of one
        # `feat!:` commit landing. The changelog still files the change under
        # "Breaking changes" (categorize() reads the commits, not this bump),
        # so the signal survives the smaller version bump.
        if major == 0:
            return f"0.{minor + 1}.0"
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------


def categorize(
    commits: Sequence[Commit], repo: str
) -> dict[str, list[tuple[str, str]]]:
    """File each commit under breaking / features / fixes / other.

    Breaking uses the same predicate as :func:`compute_bump`, so a bump the
    train prices as breaking always has a matching "Breaking changes" entry.
    """
    cats: dict[str, list[tuple[str, str]]] = {
        "breaking": [],
        "features": [],
        "fixes": [],
        "other": [],
    }
    for commit in commits:
        subject = commit.subject
        link = f"https://github.com/{repo}/commit/{commit.sha}"
        breaking = _is_breaking(commit)
        if re.match(r"ci[(!:]", subject) and not breaking:
            # `ci` touches only this repo's automation, never the released
            # package, so it is the one type left out of the notes (FND-3748).
            continue
        if breaking:
            cats["breaking"].append((link, re.sub(r"^[^:]+:\s*", "", subject)))
        elif re.match(r"feat[(!:]", subject):
            msg = re.sub(r"^feat(\([^)]+\))?!?:\s*", "", subject)
            cats["features"].append((link, msg))
        elif re.match(r"fix[(!:]", subject):
            msg = re.sub(r"^fix(\([^)]+\))?!?:\s*", "", subject)
            cats["fixes"].append((link, msg))
        else:
            cats["other"].append((link, subject))
    return cats


def format_block(new_version: str, cats: dict[str, list[tuple[str, str]]]) -> str:
    today = datetime.now(UTC).date().isoformat()
    lines = [f"## [{new_version}] - {today}", ""]
    for heading, key in [
        ("### Breaking changes", "breaking"),
        ("### Features", "features"),
        ("### Bug fixes", "fixes"),
        ("### Other changes", "other"),
    ]:
        if cats[key]:
            lines += [heading, ""]
            for link, msg in cats[key]:
                sha = link.split("/")[-1]
                lines.append(f"- {msg} ([{sha}]({link}))")
            lines.append("")
    return "\n".join(lines)


def prepend_changelog(path: str, block: str) -> None:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    else:
        existing = "# Changelog\n"
    m = re.search(r"^## \[", existing, re.MULTILINE)
    if m:
        new_content = existing[: m.start()] + block + "\n" + existing[m.start() :]
    else:
        new_content = existing.rstrip("\n") + "\n\n" + block + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _changelog_base(train: Train, current: str) -> str:
    """The revision the unreleased commits are counted from."""
    tag = train.release_tag(current)
    if tag_exists(tag):
        return tag
    # No tag for the declared version. If the train has released before, the
    # declared version was edited by hand (guard_release_files.py blocks that)
    # or the publish never tagged — either way a human has to look.
    if release_tags(train):
        sys.exit(
            f"ERROR: tag {tag!r} not found but other {train.name} release tags "
            "exist. Publish the current version first, or create the tag manually."
        )
    root = _run(["git", "rev-list", "--max-parents=0", "HEAD"])
    print(f"No {train.name} release tags found; treating as first release.")
    return root


def bump(train: Train, repo: str) -> None:
    current = read_version(train.version_file)
    base = _changelog_base(train, current)
    commits = get_commits(train, base)

    if not commits:
        print(f"No unreleased {train.name} commits (current: {current}).")
        set_output("skip", "true")
        return

    kind = compute_bump(commits)
    new_version = bump_version(current, kind)
    new_tag = train.release_tag(new_version)

    # The PR that fired this run may have merged into a *parent feature branch*
    # (a GitHub stacked PR) while the event still reads as a merge into main —
    # in which case this checkout is that feature branch, not main. Ask git
    # whether the merged commit is an ancestor of origin/main before anything
    # else; the version check below cannot tell the two apart
    # (application-sdk#3794). Empty on workflow_dispatch, which fails open.
    merged_sha = os.environ.get(release_guard.MERGE_COMMIT_ENV, "")
    landed, detail = release_guard.landed_on_branch(merged_sha)
    if not landed:
        print(release_guard.not_landed_message(merged_sha))
        set_output("skip", "true")
        return
    print(f"Merge commit check: {detail}")

    # This run may be looking at a frozen merge ref that predates an already
    # merged-and-published release, in which case `current` is stale and we are
    # about to mint a version that already exists. See release_guard for the
    # incident (application-sdk#3570) and for why the check reads the version on
    # origin/main rather than testing whether new_tag exists. Must run before
    # any file is touched.
    skip, main_version = release_guard.already_released(train.version_file, new_version)
    if skip:
        print(release_guard.skip_message(train.version_file, new_version, main_version))
        set_output("skip", "true")
        return

    print(f"Version: {current} -> {new_version} ({kind} bump)")
    write_version(train.version_file, current, new_version)

    block = format_block(new_version, categorize(commits, repo))
    prepend_changelog(train.changelog, block)
    with open(train.release_notes_file, "w", encoding="utf-8") as f:
        f.write(block)
    print(f"\nChangelog block:\n{block}")

    set_output("skip", "false")
    set_output("old", current)
    set_output("new", new_version)
    set_output("tag", new_tag)


def publish_meta(train: Train) -> None:
    """Outputs for the publish workflow: the version, its tag and the commit."""
    version = read_version(train.version_file)
    tag = train.release_tag(version)
    if not train.is_release_tag(tag):
        sys.exit(
            f"ERROR: {train.version_file} declares {version!r}; a {train.name} "
            f"release tag must be {train.tag_prefix}X.Y.Z, not {tag!r}."
        )
    merge_sha = _run(["git", "rev-parse", "HEAD"])
    set_output("version", version)
    set_output("tag", tag)
    set_output("merge_sha", merge_sha)
    print(f"Publishing {train.name} {tag} from {merge_sha}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", choices=("bump", "publish-meta"))
    parser.add_argument("train", choices=sorted(TRAINS))
    args = parser.parse_args(argv)
    train = TRAINS[args.train]
    if args.command == "bump":
        bump(train, os.environ.get("GITHUB_REPOSITORY", "atlanhq/application-sdk"))
    else:
        publish_meta(train)


if __name__ == "__main__":
    main()
