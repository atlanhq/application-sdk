"""
Extract Release Notes Script
---------------------------

Extracts the release notes for a specific version from the CHANGELOG.md file
for use in GitHub releases.

Usage: python extract_release_notes.py <version>
"""

import re
import sys


def extract_release_notes(version, changelog_file="CHANGELOG.md"):
    """
    Extract release notes for the specified version from the changelog file.

    Args:
        version (str): Version to extract notes for
        changelog_file (str): Path to the changelog file

    Returns:
        str: Release notes for the specified version
    """
    with open(changelog_file, "r") as f:
        content = f.read()

    # Look for the section for this specific version
    pattern = rf"## v{version} \([^)]+\).*?(?=## v|\Z)"
    match = re.search(pattern, content, re.DOTALL)

    if match:
        return match.group(0).strip()
    else:
        return f"Release v{version}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_release_notes.py <version>")
        sys.exit(1)

    version = sys.argv[1]
    release_notes = extract_release_notes(version)
    print(release_notes)


if __name__ == "__main__":
    main()
