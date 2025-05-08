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
        range_spec = f"{tag}..HEAD"
    else:
        old_tag = "v0.1.0-rc.1"
        # If no tag exists, get commits from beginning (as v0.1.0-rc.1)
        range_spec = f"{old_tag}..HEAD"

    # Get commits with format: <hash> <subject>
    result = subprocess.run(
        ["git", "log", range_spec, "--pretty=format:%h¦%an¦%s"],
        capture_output=True,
        text=True,
    )

    return result.stdout.strip().split("\n")


def categorize_commits(commits):
    """
    Categorize commits based on conventional commit types.

    Args:
        commits (list): List of commit messages

    Returns:
        dict: Categorized commits
    """
    categories = {"features": [], "fixes": [], "chores": [], "other": []}

    for commit in commits:
        if not commit:
            continue

        # Extract commit hash and message
        parts = commit.split("¦", 2)
        if len(parts) < 3:
            continue

        commit_hash, author_name, message_subject = parts
        commit_link = f"https://github.com/atlanhq/application-sdk/commit/{commit_hash}"

        # Categorize based on conventional commit prefixes
        if re.match(r"^feat(\(.*\))?:", message_subject) or re.match(
            r"^docs(\(.*\))?:", message_subject
        ):
            # Extract the message without the prefix
            msg = re.sub(r"^(feat|docs)(\(.*\))?:\s*", "", message_subject)
            categories["features"].append((commit_link, author_name, msg))
        elif re.match(r"^fix(\(.*\))?:", message_subject):
            msg = re.sub(r"^fix(\(.*\))?:\s*", "", message_subject)
            categories["fixes"].append((commit_link, author_name, msg))
        elif re.match(r"^chore(\(.*\))?:", message_subject) or re.match(
            r"^build(\(.*\))?:", message_subject
        ):
            msg = re.sub(r"^(chore|build)(\(.*\))?:\s*", "", message_subject)
            categories["chores"].append((commit_link, author_name, msg))
        else:
            categories["other"].append((commit_link, author_name, message_subject))

    return categories


def get_full_changelog_url(current_version, new_version):
    """
    Generate the full changelog URL for GitHub comparison.

    Args:
        current_version (str): The previous version
        new_version (str): The new version

    Returns:
        str: GitHub comparison URL
    """
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True
    )

    if result.returncode != 0:
        return ""

    # Extract repo path from Git URL
    url = result.stdout.strip()
    match = re.search(r"github\.com[:/](.+?)(.git)?$", url)
    if not match:
        return ""

    repo_path = match.group(1)
    # Remove potential .git suffix
    repo_path = repo_path.replace(".git", "")

    return f"https://github.com/{repo_path}/compare/v{current_version}...v{new_version}"


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

    # Start with the header
    changelog = f"## v{new_version} ({date_str})\n\n"

    # Add GitHub comparison URL
    full_changelog_url = get_full_changelog_url(current_version, new_version)
    if full_changelog_url:
        changelog += f"Full Changelog: {full_changelog_url}\n\n"

    # Add Features section
    if categories["features"]:
        changelog += "### Features\n\n"
        for commit_link, author_name, msg in categories["features"]:
            short_sha = commit_link.split("/")[-1][
                :7
            ]  # Extract short SHA (first 7 chars)
            changelog += (
                f"- {msg} (by @{author_name} in [{short_sha}]({commit_link}))\n"
            )
        changelog += "\n"

    # Add Fixes section
    if categories["fixes"]:
        changelog += "### Bug Fixes\n\n"
        for commit_link, author_name, msg in categories["fixes"]:
            short_sha = commit_link.split("/")[-1][
                :7
            ]  # Extract short SHA (first 7 chars)
            changelog += (
                f"- {msg} (by @{author_name} in [{short_sha}]({commit_link}))\n"
            )
        changelog += "\n"

    # NOTE: Ignore chores and other changes

    return changelog


def update_changelog_file(changelog_content):
    """
    Update the CHANGELOG.md file with new content.

    Args:
        changelog_content (str): New changelog section
    """
    changelog_path = "CHANGELOG.md"

    # If CHANGELOG.md doesn't exist or is empty, create it with initial content
    if not os.path.exists(changelog_path) or os.path.getsize(changelog_path) == 0:
        with open(changelog_path, "w") as f:
            f.write("# Changelog\n\n")
            f.write(changelog_content)
        return

    # Read existing changelog
    with open(changelog_path, "r") as f:
        existing_content = f.read()

    # Find the position to insert new content (after the title)
    title_match = re.search(r"^# Changelog", existing_content, re.MULTILINE)
    if title_match:
        insert_pos = title_match.end() + 1
        # Insert a newline if needed
        if existing_content[insert_pos : insert_pos + 2] != "\n\n":
            insert_pos += 1

        new_content = (
            existing_content[:insert_pos]
            + "\n"
            + changelog_content
            + existing_content[insert_pos:]
        )
    else:
        # If no title, just prepend the new content
        new_content = "# Changelog\n\n" + changelog_content + existing_content

    print(new_content)

    # Write updated changelog
    with open(changelog_path, "w") as f:
        f.write(new_content)


def main():
    if len(sys.argv) < 3:
        print("Usage: python update_changelog.py <current_version> <new_version>")
        sys.exit(1)

    current_version = sys.argv[1]
    new_version = sys.argv[2]

    commits = get_commits_since_last_tag(current_version)
    categories = categorize_commits(commits)
    changelog_content = format_changelog_section(
        categories, current_version, new_version
    )
    update_changelog_file(changelog_content)

    print(f"Changelog updated for version {new_version}")


if __name__ == "__main__":
    main()
