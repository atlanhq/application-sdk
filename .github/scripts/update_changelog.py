"""
SDK Changelog Generator
-----------------------

This script automatically updates the CHANGELOG.md file with changes
introduced since the last release. It categorizes commits according to
conventional commit types and creates sections that match the project's
changelog format.

Usage: python update_changelog.py <current_version> <new_version>
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime


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

_TYPE_TO_SECTION = {t: key for key, _, types in SECTIONS for t in types}
_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?:\s*(?P<desc>.*)$",
    re.IGNORECASE,
)
# Sub-packages have their own CHANGELOGs and release pipelines; exclude their
# commits from this changelog. Same predicate as release.py's _SUBPKG_RE.
_SUBPKG_RE = re.compile(r"^[a-z]+\((contract-toolkit|conformance)\)!?:")


def categorize_commits(commits):
    """
    Categorize commits based on conventional commit types.

    Args:
        commits (list): List of `sha7|author|subject` lines

    Returns:
        dict: One list of `(commit_link, author, message)` per SECTIONS key.
        `ci` commits and sub-package commits are omitted.
    """
    categories = {key: [] for key, _, _ in SECTIONS}

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

        section = _TYPE_TO_SECTION.get(commit_type, "other")
        if section == "other":
            msg = message_subject
        else:
            msg = match["desc"]
            if match["breaking"]:
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


def format_changelog_section(categories, current_version, new_version):
    """
    Format the changelog section according to the project's format.

    Args:
        categories (dict): Categorized commits
        current_version (str): The previous version
        new_version (str): The new version

    Returns:
        str: Formatted changelog section
    """
    now = datetime.now()
    date_str = now.strftime("%B %d, %Y")

    changelog = f"## v{new_version} ({date_str})\n\n"
    full_changelog_url = get_full_changelog_url(current_version, new_version)
    changelog += f"Full Changelog: {full_changelog_url}\n\n"

    for key, heading, _ in SECTIONS:
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


def update_changelog_file(changelog_content):
    """
    Update the CHANGELOG.md file with new content.

    Args:
        changelog_content (str): New changelog section
    """
    changelog_path = "CHANGELOG.md"

    if not os.path.exists(changelog_path) or os.path.getsize(changelog_path) == 0:
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write("# Changelog\n\n")
            f.write(changelog_content)
        return

    with open(changelog_path, "r", encoding="utf-8") as f:
        existing_content = f.read()

    # Find the position to insert new content (after the title)
    title_match = re.search(r"^# Changelog", existing_content, re.MULTILINE)
    if title_match:
        # Find the end of the title line
        title_end = title_match.end()

        # Skip any existing whitespace/newlines after the title
        insert_pos = title_end
        while (
            insert_pos < len(existing_content)
            and existing_content[insert_pos] in " \t\n"
        ):
            insert_pos += 1

        # Insert with consistent spacing: title + double newline + content + newline + rest
        new_content = (
            existing_content[:title_end]
            + "\n\n"
            + changelog_content
            + "\n"
            + existing_content[insert_pos:]
        )
    else:
        # If no title, just prepend the new content
        new_content = "# Changelog\n\n" + changelog_content + existing_content

    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    if len(sys.argv) < 3:
        print("Usage: python update_changelog.py <current_version> <new_version>")
        sys.exit(1)

    current_version = sys.argv[1]
    new_version = sys.argv[2]

    commits = get_commits_since_last_tag(current_version)
    if not commits:
        print("No new commits found to add to the changelog.")
        return

    categories = categorize_commits(commits)
    changelog_content = format_changelog_section(
        categories, current_version, new_version
    )
    # Print the new content to the console
    print(changelog_content)
    update_changelog_file(changelog_content)

    print(f"Changelog updated for version {new_version}")


if __name__ == "__main__":
    main()
