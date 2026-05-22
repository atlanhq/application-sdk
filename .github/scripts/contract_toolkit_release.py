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
    GITHUB_OUTPUT   - path to the GitHub Actions output file (optional for
                      local runs; falls back to printing to stdout)
    GITHUB_REPOSITORY - owner/repo (defaults to atlanhq/application-sdk)
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
    result = subprocess.run(
        ["git", "rev-parse", "--verify", tag],
        capture_output=True,
    )
    return result.returncode == 0


def commits_since_tag(tag):
    """Return (subjects, bodies) for commits since tag touching contract-toolkit/**."""
    subjects = _run([
        "git", "log", f"{tag}..HEAD",
        "--format=%s",
        "--", "contract-toolkit/**",
    ])
    bodies = _run([
        "git", "log", f"{tag}..HEAD",
        "--format=%B",
        "--", "contract-toolkit/**",
    ])
    return subjects, bodies


def compute_bump(subjects, bodies):
    if re.search(r'^[a-z]+(\([^)]+\))?!:', subjects, re.MULTILINE) or \
       re.search(r'^BREAKING[ _-]CHANGE:', bodies, re.MULTILINE):
        return "major"
    if re.search(r'^feat[(!:]', subjects, re.MULTILINE):
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

def get_pr_commits(tag):
    """
    Return (sha7, author, subject) tuples for commits since tag that actually
    touched contract-toolkit/** files.

    Strategy: git log with path filter gives us the authoritative set of SHAs.
    gh api compare enriches those with GitHub author logins. The two are joined
    on the short SHA so we never include SDK-only commits.
    """
    # Authoritative path-filtered SHAs (full 40-char so we can join reliably)
    raw_shas = _run([
        "git", "log", f"{tag}..HEAD",
        "--format=%H",
        "--", "contract-toolkit/**",
    ])
    relevant_shas = set(raw_shas.splitlines()) if raw_shas else set()

    if not relevant_shas:
        return []

    # Build short-SHA → (author, subject) from git log (always works locally)
    git_info = {}
    raw_local = _run([
        "git", "log", f"{tag}..HEAD",
        "--format=%H|%s",
        "--", "contract-toolkit/**",
    ])
    for line in raw_local.splitlines():
        if not line:
            continue
        full_sha, subject = line.split("|", 1)
        git_info[full_sha] = ("unknown", subject)

    # Try to enrich with GitHub author logins via gh api compare
    owner, repo = REPO.split("/", 1)
    range_spec = f"{tag}...HEAD"
    jq = (
        '.commits[] | '
        '"\\(.sha)|\\(.author.login // .commit.author.name)'
        '|\\(.commit.message | split("\\n")[0])"'
    )
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/compare/{range_spec}", "--jq", jq],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            full_sha, author, subject = parts
            if full_sha in relevant_shas:
                git_info[full_sha] = (author, subject)

    # Return in reverse-chronological git order, using short SHAs for links
    commits = []
    for full_sha in raw_shas.splitlines():
        if full_sha in git_info:
            author, subject = git_info[full_sha]
            commits.append((full_sha[:7], author, subject))
    return commits


def categorize(commits):
    cats = {"breaking": [], "features": [], "fixes": [], "other": []}
    owner, repo = REPO.split("/", 1)
    for sha, author, subject in commits:
        link = f"https://github.com/{owner}/{repo}/commit/{sha}"
        if re.search(r'!:', subject):
            msg = re.sub(r'^[^:]+:\s*', '', subject)
            cats["breaking"].append((link, msg))
        elif re.match(r'^feat[(!:]', subject):
            msg = re.sub(r'^feat(\([^)]+\))?[!:]?\s*:?\s*', '', subject)
            cats["features"].append((link, msg))
        elif re.match(r'^fix[(!:]', subject):
            msg = re.sub(r'^fix(\([^)]+\))?[!:]?\s*:?\s*', '', subject)
            cats["fixes"].append((link, msg))
        else:
            cats["other"].append((link, subject))
    return cats


def format_block(new_version, cats):
    today = date.today().isoformat()
    lines = [f"## [{new_version}] - {today}", ""]
    if cats["breaking"]:
        lines += ["### Breaking changes", ""]
        for link, msg in cats["breaking"]:
            sha = link.split("/")[-1]
            lines.append(f"- {msg} ([{sha}]({link}))")
        lines.append("")
    if cats["features"]:
        lines += ["### Features", ""]
        for link, msg in cats["features"]:
            sha = link.split("/")[-1]
            lines.append(f"- {msg} ([{sha}]({link}))")
        lines.append("")
    if cats["fixes"]:
        lines += ["### Bug fixes", ""]
        for link, msg in cats["fixes"]:
            sha = link.split("/")[-1]
            lines.append(f"- {msg} ([{sha}]({link}))")
        lines.append("")
    if cats["other"] and not (cats["breaking"] or cats["features"] or cats["fixes"]):
        lines += ["### Changes", ""]
        for link, msg in cats["other"]:
            sha = link.split("/")[-1]
            lines.append(f"- {msg} ([{sha}]({link}))")
        lines.append("")
    return "\n".join(lines)


def prepend_changelog(block):
    existing = open(CHANGELOG).read() if os.path.exists(CHANGELOG) else "# Changelog\n"
    m = re.search(r'^## \[', existing, re.MULTILINE)
    if m:
        new = existing[:m.start()] + block + "\n" + existing[m.start():]
    else:
        # First release: append after header
        new = existing.rstrip("\n") + "\n\n" + block + "\n"
    with open(CHANGELOG, "w") as f:
        f.write(new)


def update_pklproject(current, new):
    text = open(PKLPROJECT).read()
    updated = text.replace(f'version = "{current}"', f'version = "{new}"', 1)
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

    # Update PklProject
    update_pklproject(current, new_version)

    # Generate and prepend changelog block
    commits = get_pr_commits(tag)
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
