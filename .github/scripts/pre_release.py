"""
SDK Version Management Script for Pre-GA Releases
-------------------------------------------------

This script automates the versioning of a pre-GA SDK following PEP 440 standards.
It handles release candidate versioning (e.g., 0.1.0rc1, 0.1.0rc2) for main branch
merges to maintain organized pre-release versions without requiring manual version
management.

Why use this approach:
1. Provides clear version progression for pre-GA SDK releases
2. Maintains PEP 440 compatibility for proper PyPI integration
3. Automates RC numbering to reduce human error
4. Simplifies the release process by removing complex semantic versioning decisions
   during the pre-GA phase

Usage: python version_script.py main
"""

import logging
import subprocess
import sys

from packaging.version import parse


def bump_release_candidate(current_version: str) -> str:
    """Bump the release candidate number in a PEP 440 compliant way.

    Args:
        current_version (str): Current version string

    Returns:
        str: New version string with incremented release candidate
    """
    v = parse(current_version)

    # Get the base version (without pre-release identifiers)
    base_version = (
        f"{v.release[0]}.{v.release[1]}.{v.release[2]}"
        if len(v.release) >= 3
        else "0.1.0"
    )

    # Check if it's already a release candidate
    if v.is_prerelease and v.pre and v.pre[0] == "rc":
        # Increment RC number
        new_rc = v.pre[1] + 1
        return f"{base_version}rc{new_rc}"
    else:
        # Start with rc1
        return f"{base_version}rc1"


def update_pyproject_version(new_version: str) -> None:
    """Update the version in pyproject.toml using uv.

    Args:
        new_version (str): Version string to set in pyproject.toml

    Raises:
        RuntimeError: If uv fails to update the version
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
        error_msg = f"Failed to update version in pyproject.toml: {e.stderr if e.stderr else str(e)}"
        logging.error(error_msg)
        raise RuntimeError(error_msg)


def main():
    """Main entry point for the pre-GA versioning process."""
    enforce_on_branch = "main"
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logging.info("Starting version update process")

    current_branch = str(sys.argv[1])
    current_version = str(sys.argv[2])
    if current_branch != enforce_on_branch:
        logging.warning(
            f"Not on {enforce_on_branch} branch (on {current_branch}). Skipping version bump."
        )
        return

    new_version = bump_release_candidate(current_version)
    update_pyproject_version(new_version)


if __name__ == "__main__":
    main()
