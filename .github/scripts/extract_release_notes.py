"""
Extract Release Notes Script
---------------------------

Extracts the release notes for a specific version from the CHANGELOG.md file
for use in GitHub releases.

Usage: python extract_release_notes.py <version>
"""

import sys

from _changelog import find_section


def extract_release_notes(version, changelog_file="CHANGELOG.md"):
    """
    Extract release notes for the specified version from the changelog file.

    Args:
        version (str): Version to extract notes for
        changelog_file (str): Path to the changelog file

    Returns:
        str: The ``## v<version>`` section, heading included, or a one-line
        ``Release v<version>`` placeholder when the changelog has none.

    The version is matched exactly (``3.1.0`` never selects ``3.1.0rc1`` or
    ``301.0``), and the section ends at the next ``## `` heading line. When a
    version has two headings, the first (newest) one wins;
    ``lint_changelog.py`` stops the release workflow from writing a second.
    """
    with open(changelog_file, "r", encoding="utf-8") as f:
        content = f.read()

    section = find_section(content, version)
    if section is None:
        return f"Release v{version}"
    return section.text.strip()


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_release_notes.py <version>")
        sys.exit(1)

    version = sys.argv[1]
    release_notes = extract_release_notes(version)
    print(release_notes)


if __name__ == "__main__":
    main()
