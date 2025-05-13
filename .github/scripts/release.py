import logging
import re
import subprocess
import sys
from typing import List, Tuple

import semver


def get_commits_since_last_tag() -> List[str]:
    """Get all commits since the last non release-candidate tag.

    Returns:
        List[str]:  List of commit messages since the last git non release-candidate tag.
                    If no tags exist, returns commits since the initial commit.
    """
    try:
        # Get the last non release-candidate tag or initial commit if no tags exist
        last_tag_cmd = "git describe --tags --abbrev=0 --exclude='*-rc*' 2>/dev/null || git rev-list --max-parents=0 HEAD"
        last_tag = subprocess.check_output(last_tag_cmd, shell=True).decode().strip()
        logging.info(f"Last tag found: {last_tag}")

        # Get all commits since that reference, including both subject and description
        cmd = f"git log {last_tag}..HEAD --pretty=format:%s%n%b"
        commits = subprocess.check_output(cmd, shell=True).decode().strip().split("\n")
        # Filter out empty lines that may appear between commits
        commits = [commit for commit in commits if commit.strip()]
        logging.info(f"Found {len(commits)} commits since last tag: {last_tag}")
        return commits

    except subprocess.CalledProcessError as e:
        logging.error(f"Error retrieving commits: {e}")
        raise e


def parse_conventional_commits(commits: List[str]) -> Tuple[bool, bool, bool]:
    """Parse conventional commit messages to determine version bump type.

    Args:
        commits (List[str]): List of commit messages to analyze.

    Returns:
        Tuple[bool, bool, bool]: A tuple containing three flags:
            - is_breaking: True if breaking changes are detected
            - is_feature: True if new features are detected
            - is_fix: True if bug fixes are detected
    """
    logging.info(f"Parsing {len(commits)} conventional commits")
    is_breaking = False
    is_feature = False
    is_fix = False

    breaking_pattern = "!:"
    breaking_change = "BREAKING CHANGE:"
    feature_pattern = "feat"
    fix_pattern = "fix"

    for commit in commits:
        if re.search(breaking_pattern, commit, re.MULTILINE | re.IGNORECASE):
            is_breaking = True
        elif re.search(breaking_change, commit, re.MULTILINE | re.IGNORECASE):
            is_breaking = True
        elif re.search(feature_pattern, commit, re.IGNORECASE):
            is_feature = True
        elif re.search(fix_pattern, commit, re.IGNORECASE):
            is_fix = True

    logging.info(
        f"Commit analysis results - Breaking: {is_breaking}, Feature: {is_feature}, Fix: {is_fix}"
    )
    return is_breaking, is_feature, is_fix


def calculate_version_bump(
    current_version: str, commits: List[str], current_branch: str
) -> str:
    """Calculate the next version based on conventional commits, semver rules, and branch name.

    Args:
        current_version (str): Current version string (e.g., "1.0.0")
        commits (List[str]): List of conventional commit messages

    Returns:
        str: New version string based on conventional commit analysis and branch name.
            Returns current version if no bump is needed.
    """
    logging.info(
        f"Calculating version bump from {current_version} for {current_branch}"
    )
    version = semver.VersionInfo.parse(current_version)

    if current_branch == "develop":
        if version.prerelease:
            new_version = version.bump_prerelease()
            logging.info(f"Bumping pre-release from {version} to {new_version}")
            return str(new_version)
        else:
            new_version = version.bump_patch().bump_prerelease()
            logging.info(
                f"Bumping patch and pre-release from {version} to {new_version}"
            )
            return str(new_version)
    elif current_branch == "main":
        is_breaking, is_feature, is_fix = parse_conventional_commits(commits=commits)
        logging.info(f"Breaking: {is_breaking}, Feature: {is_feature}, Fix: {is_fix}")

        if is_breaking:
            new_version = version.bump_major()
            logging.info(
                f"Breaking change detected - bumping major version to {new_version}"
            )
        elif is_feature:
            new_version = version.bump_minor()
            logging.info(f"Feature detected - bumping minor version to {new_version}")
        elif is_fix:
            # Patch was already bumped in the develop branch
            new_version = version.next_version(part="patch")
            logging.info(f"Fix detected - bumping version to {new_version}")
        else:
            # No changes were detected in the commits, remove the prerelease, as patch was already bumped in the develop branch
            new_version = version.next_version(part="patch")
            logging.info(
                f"No changes detected - bumping patch version to {new_version}"
            )

        return str(new_version)
    else:
        logging.warning(f"Unexpected branch '{current_branch}'. Using current version.")
        return current_version


def update_pyproject_version(new_version: str) -> None:
    """Update the version in pyproject.toml using uv.

    Args:
        new_version (str): Version string to set in pyproject.toml

    Raises:
        subprocess.CalledProcessError: If uv fails to update the version
    """
    logging.info(f"Updating pyproject.toml version to {new_version}")
    try:
        subprocess.run(
            [
                "uvx",
                "--from=toml-cli",
                "toml",
                "set",
                "--toml-path=pyproject.toml",
                "project.version",
                new_version,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        logging.info("Successfully updated pyproject.toml version")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to update version in pyproject.toml: {e}")
        raise


def main():
    """Main entry point for the version update process.

    Sets up logging and orchestrates the version update workflow:
    1. Gets current version
    2. Retrieves commits since last tag
    3. Calculates version bump
    4. Updates pyproject.toml with new version
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logging.info("Starting version update process")

    current_branch = str(sys.argv[1])
    current_version = str(sys.argv[2])
    commits = get_commits_since_last_tag()

    new_version = calculate_version_bump(
        current_version=current_version, commits=commits, current_branch=current_branch
    )

    update_pyproject_version(new_version=new_version)


if __name__ == "__main__":
    main()
