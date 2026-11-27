"""
SDK Changelog Generator
-----------------------

This script automatically updates the CHANGELOG.md file with changes
introduced since the last release. It categorizes commits according to
conventional commit types and creates sections that match the project's
changelog format.

Usage: python update_changelog.py <current_version> <new_version>
       [--sections all|legacy|auto] [--archive-dir DIR]

With --archive-dir, every section from a major older than <new_version>'s is
moved out of CHANGELOG.md into DIR/v<major>.md (see archive_older_majors).
"""

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from _changelog import split_sections
from conventional_breaking import declares_breaking_change


def _get_repo() -> tuple[str, str]:
    """Read the repo from the environment so this script works for any
    downstream app (GITHUB_REPOSITORY is always set in GitHub Actions; default
    to application-sdk for local runs)."""
    owner, repo = os.environ.get("GITHUB_REPOSITORY", "atlanhq/application-sdk").split(
        "/", 1
    )
    return owner, repo


def _gh_api_commits(path: str) -> list[dict]:
    """Call `gh api <path>` (paginating) and return the decoded JSON list.

    The path must resolve to a JSON array of commit objects. Raises
    RuntimeError when the call fails or the token is missing — a changelog
    written from zero commits is silently empty, and an empty first-release
    changelog ships looking like a success.
    """
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        raise RuntimeError(
            "GH_TOKEN is not set — cannot list commits for the changelog. "
            "Set GH_TOKEN (the release workflow does) and re-run."
        )

    result = subprocess.run(
        ["gh", "api", "--paginate", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()}")

    # --paginate concatenates one JSON array per page, so decode line by line.
    commits = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            commits.extend(json.loads(line))
    return commits


def _format_commits(commits: list[dict]) -> list[str]:
    """Render commit objects as `sha7|author|subject` lines (oldest first)."""
    lines = []
    for commit in reversed(commits):
        author = (commit.get("author") or {}).get("login") or (
            commit.get("commit", {}).get("author") or {}
        ).get("name", "")
        subject = commit.get("commit", {}).get("message", "").split("\n")[0]
        lines.append(f"{commit['sha'][:7]}|{author}|{subject}")
    return lines


def get_commits_since_last_tag(current_version):
    """
    Get all commits since the last tag.

    Args:
        current_version (str): The current version string

    Returns:
        list: A list of commit messages

    When the tag exists, uses the compare API (`v{current}...HEAD`). When it
    does not — a repository's first release — enumerates the full history via
    the commits API instead. The previous fallback built a range from
    `git rev-list --max-parents=0 HEAD`, which emits one SHA per root commit:
    on a multi-root repo the multi-line value makes the compare call fail
    (net/url: invalid control character), and the old `[]`-on-error path then
    shipped the first-ever release with an empty changelog and a green
    workflow. On a single-root untagged repo it silently omitted the root
    commit from the range.
    """
    tag = f"v{current_version}"

    # Check if tag exists
    result = subprocess.run(["git", "tag", "-l", tag], capture_output=True, text=True)

    owner, repo = _get_repo()

    if tag in result.stdout:
        # Standard pipe '|' delimiter to avoid encoding issues.
        jq_filter = '.commits[] | "\\(.sha[0:7])|\\(.author.login // .commit.author.name)|\\(.commit.message | split("\\n")[0])"'
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/compare/{tag}...HEAD",
                "--jq",
                jq_filter,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh api compare failed: {result.stderr.strip()}")
        return result.stdout.strip().split("\n") if result.stdout.strip() else []

    # Untagged: first release — walk every commit reachable from HEAD.
    return _format_commits(_gh_api_commits(f"repos/{owner}/{repo}/commits?sha=HEAD"))


# Changelog sections in render order: (key, heading, conventional types).
# Every conventional type except `ci` is rendered (FND-3748): the release
# changelog is the human review gate for a semver release, so any change to
# what ships must be visible in it — a large `perf:` or `refactor:` PR used to
# leave the notes empty. `ci` alone is dropped, because it only touches the
# repo's own automation, never the released artefact.
SECTIONS = (
    ("features", "Features", ("feat",)),
    ("fixes", "Bug Fixes", ("fix",)),
    ("performance", "Performance", ("perf",)),
    ("refactors", "Refactoring", ("refactor",)),
    ("docs", "Documentation", ("docs",)),
    ("chores", "Chores", ("chore", "build")),
    # Anything else lands here with its full subject, so a non-conventional or
    # unknown-type subject is still listed rather than silently dropped.
    ("other", "Other Changes", ()),
)
EXCLUDED_TYPES = frozenset({"ci"})

# The notes as they were before FND-3748: Features (feat, docs) and Bug Fixes
# only. Kept for app repos whose CI shims are not yet gated on `gate-check`
# (see sections_for_repo): rendering chores there would move the bump PR on
# every Renovate merge and re-run its full, still-ungated CI each time.
LEGACY_SECTIONS = (
    ("features", "Features", ("feat", "docs")),
    ("fixes", "Bug Fixes", ("fix",)),
)

# What marks a repo whose bump-PR CI is gated on the opt-in: the bootstrap
# shims carry one once the repo has resynced onto the FND-3748 templates, and
# they all arrive in the same resync. `'gate-check'` is the label-era gate.
# From FND-3839 the shims skipped the bump PR's own run with a caller `if:`;
# since FND-3947 they pass `skip-on-bump-pr: true` to the reusable instead.
OPT_IN_MARKERS = (
    "'gate-check'",
    "!startsWith(github.head_ref, 'bump-version')",
    "skip-on-bump-pr: true",
)
_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?:\s*(?P<desc>.*)$",
    re.IGNORECASE,
)
# Sub-packages have their own CHANGELOGs and release pipelines; exclude their
# commits from this changelog. Same predicate as release.py's _SUBPKG_RE.
_SUBPKG_RE = re.compile(r"^[a-z]+\((contract-toolkit|conformance)\)!?:")


def sections_for_repo(root="."):
    """Pick SECTIONS or LEGACY_SECTIONS for the repo checked out at ``root``.

    Full notes only once the repo's own workflows gate bump-PR CI on
    `gate-check`, so the extra bump-PR pushes they cause never run ahead of
    that gate: each app repo switches over in the same resync that gates it.
    """
    workflows = Path(root) / ".github" / "workflows"
    for path in sorted(workflows.glob("*.y*ml")) if workflows.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in OPT_IN_MARKERS):
                return SECTIONS
        except OSError:
            continue
    return LEGACY_SECTIONS


def categorize_commits(
    commits: list[str],
    sections: tuple[tuple[str, str, tuple[str, ...]], ...] = SECTIONS,
) -> dict[str, list[tuple[str, str, str]]]:
    """
    Categorize commits based on conventional commit types.

    Args:
        commits (list): List of `sha7|author|subject` lines
        sections: SECTIONS or LEGACY_SECTIONS

    Returns:
        dict: One list of `(commit_link, author, message)` per section key.
        `ci` commits and sub-package commits are omitted, as is any type the
        sections don't render (everything outside feat/docs/fix in legacy).
    """
    categories: dict[str, list[tuple[str, str, str]]] = {
        key: [] for key, _, _ in sections
    }
    type_to_section: dict[str | None, str] = {
        t: key for key, _, types in sections for t in types
    }

    owner, repo = _get_repo()

    for commit in commits:
        if not commit:
            continue

        # Use the corrected delimiter
        parts = commit.split("|", 2)
        if len(parts) < 3:
            continue

        commit_hash, author_name, message_subject = parts

        if _SUBPKG_RE.match(message_subject):
            continue

        commit_link = f"https://github.com/{owner}/{repo}/commit/{commit_hash}"

        match = _CONVENTIONAL_RE.match(message_subject)
        commit_type = match["type"].lower() if match else None
        if commit_type in EXCLUDED_TYPES:
            continue

        section = type_to_section.get(commit_type, "other")
        if section not in categories:
            continue
        msg = message_subject if section == "other" else match["desc"]
        # The predicate release.py prices a major with, so every major's notes
        # say which commit made it one (FND-4672).
        if declares_breaking_change(message_subject):
            msg = f"**BREAKING:** {msg}"
        categories[section].append((commit_link, author_name, msg))

    return categories


def get_full_changelog_url(current_version, new_version):
    """
    Generate the full changelog URL for GitHub comparison.
    """
    owner, repo = _get_repo()
    return (
        f"https://github.com/{owner}/{repo}/compare/v{current_version}...v{new_version}"
    )


def format_changelog_section(
    categories, current_version, new_version, sections=SECTIONS
):
    """
    Format the changelog section according to the project's format.

    Args:
        categories (dict): Categorized commits
        current_version (str): The previous version
        new_version (str): The new version

    Returns:
        str: Formatted changelog section
    """
    now = datetime.now(UTC)
    date_str = now.strftime("%B %d, %Y")

    changelog = f"## v{new_version} ({date_str})\n\n"
    full_changelog_url = get_full_changelog_url(current_version, new_version)
    changelog += f"Full Changelog: {full_changelog_url}\n\n"

    for key, heading, _ in sections:
        if not categories[key]:
            continue
        changelog += f"### {heading}\n\n"
        for commit_link, author_name, msg in categories[key]:
            short_sha = commit_link.split("/")[-1][:7]
            changelog += (
                f"- {msg} (by @{author_name} in [{short_sha}]({commit_link}))\n"
            )
        changelog += "\n"

    return changelog


def update_changelog_file(
    changelog_content: str, changelog_path: str = "CHANGELOG.md"
) -> None:
    """
    Prepend a new section to the changelog, below its title and preamble.

    Args:
        changelog_content (str): New changelog section
        changelog_path (str): The changelog to update

    The section goes immediately before the first ``## `` heading, so anything
    between ``# Changelog`` and the first release (the archive pointer that
    ``archive_older_majors`` writes) stays at the top.
    """
    if not os.path.exists(changelog_path) or os.path.getsize(changelog_path) == 0:
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write("# Changelog\n\n")
            f.write(changelog_content)
        return

    with open(changelog_path, "r", encoding="utf-8") as f:
        existing_content = f.read()

    title_match = re.search(r"^# Changelog", existing_content, re.MULTILINE)
    if not title_match:
        # If no title, just prepend the new content
        new_content = "# Changelog\n\n" + changelog_content + existing_content
    else:
        preamble, sections = split_sections(existing_content)
        # Title line plus any preamble text, normalised to end in one blank line.
        head = preamble.rstrip() + "\n\n"
        rest = "".join(section.text for section in sections)
        new_content = head + changelog_content + "\n" + rest

    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(new_content)


#: The line ``archive_older_majors`` puts under ``# Changelog``. Written once;
#: its presence is how a later archive knows not to add it again.
ARCHIVE_POINTER = (
    "Releases from earlier major versions are archived in "
    "[docs/changelog/](https://github.com/{owner}/{repo}/tree/main/{archive_dir}), "
    "one file per major."
)


def _archive_title(major: int) -> str:
    return f"# Changelog — v{major}.x (archived)\n\n"


def archive_older_majors(
    new_version: str,
    archive_dir: str | Path,
    changelog_path: str | Path = "CHANGELOG.md",
) -> list[Path]:
    """Move every section older than ``new_version``'s major out of the changelog.

    Each moved section is prepended, unchanged and in its original order, to
    ``<archive_dir>/v<major>.md``, and CHANGELOG.md gets a one-line pointer to
    the archive under its title. CHANGELOG.md is left holding only the current
    major's releases.

    Runs on every release, so it is idempotent: with nothing from an older major
    left in CHANGELOG.md it changes nothing, and in steady state it moves
    anything only on the release that bumps the major. Sections whose heading
    has no numeric version stay where they are.

    Returns the archive files it wrote (empty when nothing moved).
    """
    version_match = re.match(r"v?(\d+)", new_version)
    if version_match is None:
        raise ValueError(f"cannot read a major version from {new_version!r}")
    new_major = int(version_match.group(1))
    changelog_path = Path(changelog_path)
    archive_dir = Path(archive_dir)

    preamble, sections = split_sections(changelog_path.read_text(encoding="utf-8"))
    keep = [s for s in sections if s.major is None or s.major >= new_major]
    move = [s for s in sections if s.major is not None and s.major < new_major]
    moved_majors = sorted({s.major for s in move if s.major is not None}, reverse=True)
    if not move:
        return []

    written: list[Path] = []
    archive_dir.mkdir(parents=True, exist_ok=True)
    for major in moved_majors:
        path = archive_dir / f"v{major}.md"
        moved = "".join(s.text for s in move if s.major == major)
        if not moved.endswith("\n\n"):
            moved = moved.rstrip("\n") + "\n\n"
        existing = ""
        if path.exists():
            existing = "".join(
                s.text for s in split_sections(path.read_text(encoding="utf-8"))[1]
            )
        path.write_text(
            _archive_title(major) + (moved + existing).rstrip("\n") + "\n",
            encoding="utf-8",
        )
        written.append(path)

    owner, repo = _get_repo()
    try:
        # The pointer is a repo path: relative to the changelog's directory.
        link_dir = archive_dir.resolve().relative_to(changelog_path.resolve().parent)
    except ValueError:
        link_dir = archive_dir
    pointer = ARCHIVE_POINTER.format(
        owner=owner, repo=repo, archive_dir=link_dir.as_posix()
    )
    head = preamble.rstrip("\n")
    if pointer not in head:
        head = f"{head}\n\n{pointer}"
    rest = "".join(s.text for s in keep).rstrip("\n")
    changelog_path.write_text(
        head + "\n\n" + rest + "\n" if rest else head + "\n", encoding="utf-8"
    )
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Prepend a CHANGELOG section.")
    parser.add_argument("current_version")
    parser.add_argument("new_version")
    parser.add_argument(
        "--sections",
        choices=("all", "legacy", "auto"),
        default="all",
        help=(
            "all: every type but ci (default). legacy: Features + Bug Fixes "
            "only. auto: all once this repo's workflows gate bump-PR CI on "
            "`gate-check`, else legacy (what fleet app repos pass)."
        ),
    )
    parser.add_argument(
        "--archive-dir",
        default="",
        help=(
            "Move every section older than the new version's major into "
            "<dir>/v<major>.md after prepending the new one. Off by default: "
            "only the SDK's own release.yaml passes it, because the fleet's "
            "release-version-bump.yaml hands over CHANGELOG.md alone."
        ),
    )
    args = parser.parse_args(argv)
    current_version = args.current_version
    new_version = args.new_version
    sections = {
        "all": SECTIONS,
        "legacy": LEGACY_SECTIONS,
    }.get(args.sections) or sections_for_repo()
    print(
        "Changelog sections: "
        + ", ".join(heading for _, heading, _ in sections)
        + f" ({args.sections})"
    )

    commits = get_commits_since_last_tag(current_version)
    if not commits:
        print("No new commits found to add to the changelog.")
        return

    categories = categorize_commits(commits, sections)
    changelog_content = format_changelog_section(
        categories, current_version, new_version, sections
    )
    # Print the new content to the console
    print(changelog_content)
    update_changelog_file(changelog_content)
    if args.archive_dir:
        for path in archive_older_majors(new_version, args.archive_dir):
            print(f"Archived older-major sections to {path}")

    print(f"Changelog updated for version {new_version}")


if __name__ == "__main__":
    main()
