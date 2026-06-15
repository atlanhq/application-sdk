"""
conformance_release.py
----------------------
Computes the next semver for atlan-application-sdk-conformance, updates
packages/conformance/pyproject.toml and conformance/__init__.py, and
prepends a new section to packages/conformance/CHANGELOG.md.

Usage:
    python conformance_release.py

Exits 0 with "skip=true" written to GITHUB_OUTPUT when there are no
unreleased commits touching packages/conformance/**. Exits non-zero on error.

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
PYPROJECT = "packages/conformance/pyproject.toml"
VERSION_PY = "packages/conformance/conformance/__init__.py"
CHANGELOG = "packages/conformance/CHANGELOG.md"
RELEASE_NOTES_FILE = "/tmp/conformance-release-notes.md"
TAG_PREFIX = "conformance-v"
PATH_FILTER = "packages/conformance/**"


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
    text = open(PYPROJECT).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        sys.exit(f"ERROR: could not parse version from {PYPROJECT}")
    return m.group(1)


def tag_exists(tag):
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", tag], capture_output=True
        ).returncode
        == 0
    )


def commits_since_tag(tag):
    """Return (subjects, bodies) for commits since tag touching packages/conformance/**."""
    subjects = _run(
        [
            "git",
            "log",
            f"{tag}..HEAD",
            "--format=%s",
            "--",
            PATH_FILTER,
        ]
    )
    bodies = _run(
        [
            "git",
            "log",
            f"{tag}..HEAD",
            "--format=%B",
            "--",
            PATH_FILTER,
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
# Changelog generation
# ---------------------------------------------------------------------------


def get_commits(tag):
    raw = _run(
        [
            "git",
            "log",
            f"{tag}..HEAD",
            "--format=%H%x00%s%x00%b%x1e",
            "--",
            PATH_FILTER,
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


def update_pyproject(current, new):
    text = open(PYPROJECT).read()
    old_str = f'version = "{current}"'
    new_str = f'version = "{new}"'
    if old_str not in text:
        sys.exit(
            f"ERROR: could not find {old_str!r} in {PYPROJECT} — "
            "version string format may have changed."
        )
    with open(PYPROJECT, "w") as f:
        f.write(text.replace(old_str, new_str, 1))


def update_version_py(current, new):
    text = open(VERSION_PY).read()
    old_str = f'__version__ = "{current}"'
    new_str = f'__version__ = "{new}"'
    if old_str not in text:
        sys.exit(
            f"ERROR: could not find {old_str!r} in {VERSION_PY} — "
            "version string format may have changed."
        )
    with open(VERSION_PY, "w") as f:
        f.write(text.replace(old_str, new_str, 1))


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
        print(f"No unreleased conformance commits (current: {current}).")
        _set_output("skip", "true")
        return

    bump = compute_bump(subjects, bodies)
    new_version = bump_version(current, bump)
    new_tag = f"{TAG_PREFIX}{new_version}"

    print(f"Version: {current} -> {new_version} ({bump} bump)")

    update_pyproject(current, new_version)
    update_version_py(current, new_version)

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
