"""
SDK Changelog Generator
-----------------------

This script automatically updates the CHANGELOG.md file with changes
introduced since the last release. It categorizes commits according to
conventional commit types and creates sections that match the project's
changelog format.

Usage: python update_changelog.py <current_version> <new_version>
"""

import os
import re
import subprocess
import sys
from datetime import datetime


def get_commits_since_last_tag(current_version):
    """
    Get all commits since the last tag.

    Args:
        current_version (str): The current version string

    Returns:
        list: A list of commit messages
    """
    tag = f"v{current_version}"

    # Check if tag exists
    result = subprocess.run(["git", "tag", "-l", tag], capture_output=True, text=True)

    if tag in result.stdout:
        range_spec = f"{tag}...HEAD"
    else:
        old_tag = "v0.1.0-rc.1"
        # If no tag exists, get commits from beginning (as v0.1.0-rc.1)
        range_spec = f"{old_tag}...HEAD"

    # Hardcode to the correct repo to fetch commits from
    owner, repo = "atlanhq", "application-sdk"
    compare_path = f"repos/{owner}/{repo}/compare/{range_spec}"

    # Use a standard pipe '|' delimiter to avoid encoding issues.
    jq_filter = '.commits[] | "\\(.sha[0:7])|\\(.author.login // .commit.author.name)|\\(.commit.message | split("\\n")[0])"'

    result = subprocess.run(
        ["gh", "api", compare_path, "--jq", jq_filter],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if result.returncode != 0:
        print(f"Error calling GitHub API: {result.stderr}")
        return []

    return result.stdout.strip().split("\n") if result.stdout.strip() else []


def categorize_commits(commits):
    """
    Categorize commits based on conventional commit types.

    Args:
        commits (list): List of commit messages

    Returns:
        dict: Categorized commits
    """
    categories = {"features": [], "fixes": [], "chores": [], "other": []}

    # Hardcode to the correct repo for generating commit links
    owner, repo = "atlanhq", "application-sdk"

    for commit in commits:
        if not commit:
            continue

        # Use the corrected delimiter
        parts = commit.split("|", 2)
        if len(parts) < 3:
            continue

        commit_hash, author_name, message_subject = parts
        commit_link = f"https://github.com/{owner}/{repo}/commit/{commit_hash}"

        if re.match(r"^(feat|docs)(\(.*\))?:", message_subject):
            msg = re.sub(r"^(feat|docs)(\(.*\))?:\s*", "", message_subject)
            categories["features"].append((commit_link, author_name, msg))
        elif re.match(r"^fix(\(.*\))?:", message_subject):
            msg = re.sub(r"^fix(\(.*\))?:\s*", "", message_subject)
            categories["fixes"].append((commit_link, author_name, msg))
        elif re.match(r"^(chore|build)(\(.*\))?:", message_subject):
            msg = re.sub(r"^(chore|build)(\(.*\))?:\s*", "", message_subject)
            categories["chores"].append((commit_link, author_name, msg))
        else:
            categories["other"].append((commit_link, author_name, message_subject))

    return categories


def get_full_changelog_url(current_version, new_version):
    """
    Generate the full changelog URL for GitHub comparison.
    """
    # Hardcode to the correct repo for generating the full changelog link
    owner, repo = "atlanhq", "application-sdk"
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

    if categories["features"]:
        changelog += "### Features\n\n"
        for commit_link, author_name, msg in categories["features"]:
            short_sha = commit_link.split("/")[-1][:7]
            changelog += (
                f"- {msg} (by @{author_name} in [{short_sha}]({commit_link}))\n"
            )
        changelog += "\n"

    if categories["fixes"]:
        changelog += "### Bug Fixes\n\n"
        for commit_link, author_name, msg in categories["fixes"]:
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
