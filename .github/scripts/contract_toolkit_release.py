"""
contract_toolkit_release.py
---------------------------
Computes the next semver for app-contract-toolkit, updates
contract-toolkit/src/PklProject, and prepends a new section to
contract-toolkit/CHANGELOG.md.

Usage:
    python contract_toolkit_release.py

Exits 0 with "skip=true" written to GITHUB_OUTPUT when there are no
unreleased commits touching contract-toolkit/**. Exits non-zero on error.

Environment:
    GITHUB_OUTPUT      - path to the GitHub Actions output file (optional for
                         local runs; falls back to printing to stdout)
    GITHUB_REPOSITORY  - owner/repo (defaults to atlanhq/application-sdk)
"""

import os
import re
import subprocess
import sys
from datetime import date

REPO = os.environ.get("GITHUB_REPOSITORY", "atlanhq/application-sdk")
PKLPROJECT = "contract-toolkit/src/PklProject"
CHANGELOG = "contract-toolkit/CHANGELOG.md"
RELEASE_NOTES_FILE = "/tmp/contract-toolkit-release-notes.md"
TAG_PREFIX = "contract-toolkit-v"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd, **kwargs):
    return subprocess.check_output(cmd, text=True, **kwargs).strip()


def _set_output(key, value):
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"OUTPUT: {key}={value}")


def read_current_version():
    text = open(PKLPROJECT).read()
    m = re.search(r'version\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit(f"ERROR: could not parse version from {PKLPROJECT}")
    return m.group(1)


def tag_exists(tag):
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", tag], capture_output=True
        ).returncode
        == 0
    )


def commits_since_tag(tag):
    """Return (subjects, bodies) for commits since tag touching contract-toolkit/**."""
    subjects = _run(
        [
            "git",
            "log",
            f"{tag}..HEAD",
            "--format=%s",
            "--",
            "contract-toolkit/**",
        ]
    )
    bodies = _run(
        [
            "git",
            "log",
            f"{tag}..HEAD",
            "--format=%B",
            "--",
            "contract-toolkit/**",
        ]
    )
    return subjects, bodies


def compute_bump(subjects, bodies):
    if re.search(r"^[a-z]+(\([^)]+\))?!:", subjects, re.MULTILINE) or re.search(
        r"^BREAKING[ _-]CHANGE:", bodies, re.MULTILINE
    ):
        return "major"
    if re.search(r"^feat[(!:]", subjects, re.MULTILINE):
        return "minor"
    return "patch"


def bump_version(current, bump):
    major, minor, patch = (int(x) for x in current.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# ---------------------------------------------------------------------------
# Changelog generation (matches existing ## [x.y.z] - date format)
# ---------------------------------------------------------------------------


def get_commits(tag):
    """
    Return (sha7, subject, body) tuples for commits since tag that touched
    contract-toolkit/** files, in reverse-chronological order.

    Uses git log with path filter as the authoritative source — no network
    calls needed.
    """
    raw = _run(
        [
            "git",
            "log",
            f"{tag}..HEAD",
            "--format=%H%x00%s%x00%b%x1e",  # NUL-delimited fields, RS record separator
            "--",
            "contract-toolkit/**",
        ]
    )
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x00", 2)
        if len(parts) < 2:
            continue
        full_sha = parts[0].strip()
        subject = parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        commits.append((full_sha[:7], subject, body))
    return commits


def categorize(commits):
    """
    Categorise each commit into breaking / features / fixes / other.

    Breaking changes are detected from both the subject (! marker) and the
    commit body (BREAKING CHANGE: trailer) so that a major bump always has
    corresponding release notes.
    """
    cats = {"breaking": [], "features": [], "fixes": [], "other": []}
    owner, repo = REPO.split("/", 1)
    for sha, subject, body in commits:
        link = f"https://github.com/{owner}/{repo}/commit/{sha}"
        is_breaking = bool(re.search(r"!:", subject)) or bool(
            re.search(r"^BREAKING[ _-]CHANGE:", body, re.MULTILINE)
        )
        if is_breaking:
            msg = re.sub(r"^[^:]+:\s*", "", subject)
            cats["breaking"].append((link, msg))
        elif re.match(r"^feat[(!:]", subject):
            msg = re.sub(r"^feat(\([^)]+\))?!?:\s*", "", subject)
            cats["features"].append((link, msg))
        elif re.match(r"^fix[(!:]", subject):
            msg = re.sub(r"^fix(\([^)]+\))?!?:\s*", "", subject)
            cats["fixes"].append((link, msg))
        else:
            cats["other"].append((link, subject))
    return cats


def format_block(new_version, cats):
    today = date.today().isoformat()
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


def prepend_changelog(block):
    existing = open(CHANGELOG).read() if os.path.exists(CHANGELOG) else "# Changelog\n"
    m = re.search(r"^## \[", existing, re.MULTILINE)
    if m:
        new_content = existing[: m.start()] + block + "\n" + existing[m.start() :]
    else:
        new_content = existing.rstrip("\n") + "\n\n" + block + "\n"
    with open(CHANGELOG, "w") as f:
        f.write(new_content)


def update_pklproject(current, new):
    text = open(PKLPROJECT).read()
    old_str = f'version = "{current}"'
    new_str = f'version = "{new}"'
    updated = text.replace(old_str, new_str, 1)
    if old_str not in text:
        sys.exit(
            f"ERROR: could not find {old_str!r} in {PKLPROJECT} — "
            "version string format may have changed."
        )
    with open(PKLPROJECT, "w") as f:
        f.write(updated)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    current = read_current_version()
    tag = f"{TAG_PREFIX}{current}"

    if not tag_exists(tag):
        sys.exit(
            f"ERROR: tag {tag} not found. "
            "Publish the current version first, or create the tag manually."
        )

    subjects, bodies = commits_since_tag(tag)

    if not subjects.strip():
        print(f"No unreleased contract-toolkit commits (current: {current}).")
        _set_output("skip", "true")
        return

    bump = compute_bump(subjects, bodies)
    new_version = bump_version(current, bump)
    new_tag = f"{TAG_PREFIX}{new_version}"

    print(f"Version: {current} -> {new_version} ({bump} bump)")

    update_pklproject(current, new_version)

    commits = get_commits(tag)
    cats = categorize(commits)
    block = format_block(new_version, cats)
    prepend_changelog(block)

    with open(RELEASE_NOTES_FILE, "w") as f:
        f.write(block)

    print(f"\nChangelog block:\n{block}")

    _set_output("skip", "false")
    _set_output("old", current)
    _set_output("new", new_version)
    _set_output("tag", new_tag)


if __name__ == "__main__":
    main()
