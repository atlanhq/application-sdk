"""Tests for .github/scripts/extract_release_notes.py and the _changelog reader.

The extractor used to interpolate the version into its regex unescaped and end
a section at any `## v` substring, so `3.1.0` could select `3.1.0rc1` or
`301.0`, and a duplicate heading was resolved silently. These pin the exact
match, the heading-line boundary and the byte-exact split the archive relies on.
"""

from __future__ import annotations

from pathlib import Path

import _changelog
import pytest
from extract_release_notes import extract_release_notes

CHANGELOG = """# Changelog

## v301.0 (May 03, 2026)

- wrong: a dot in the version must not match any character (#3)

## v3.1.0rc1 (May 02, 2026)

- wrong: a prefix match (#2)

## v3.1.0 (May 01, 2026)

### Features

- right one (#1) — mentions ## v9.9.9 mid-line, which is not a heading

## v3.0.0 (April 19, 2026)

- newer duplicate (#11)

## v3.0.0 (April 18, 2026)

- older duplicate (#10)
"""


def _write(tmp_path: Path, text: str = CHANGELOG) -> str:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("version", ["3.1.0", "v3.1.0"])
def test_exact_version_match(tmp_path: Path, version: str) -> None:
    notes = extract_release_notes(version, _write(tmp_path))
    assert notes.startswith("## v3.1.0 (May 01, 2026)")
    assert "right one (#1)" in notes
    assert "wrong" not in notes


def test_section_ends_at_next_heading_line_not_substring(tmp_path: Path) -> None:
    notes = extract_release_notes("3.1.0", _write(tmp_path))
    assert "## v9.9.9 mid-line" in notes
    assert notes.endswith("not a heading")


def test_duplicate_heading_returns_the_newest(tmp_path: Path) -> None:
    notes = extract_release_notes("3.0.0", _write(tmp_path))
    assert "newer duplicate" in notes
    assert "older duplicate" not in notes


def test_missing_version_falls_back(tmp_path: Path) -> None:
    assert extract_release_notes("9.9.9", _write(tmp_path)) == "Release v9.9.9"


def test_last_section_runs_to_end_of_file(tmp_path: Path) -> None:
    sections = _changelog.sections_for(CHANGELOG, "3.0.0")
    assert sections[-1].text.endswith("- older duplicate (#10)\n")


def test_split_is_byte_exact() -> None:
    preamble, sections = _changelog.split_sections(CHANGELOG)
    assert preamble == "# Changelog\n\n"
    assert preamble + "".join(s.text for s in sections) == CHANGELOG
    assert [s.version for s in sections] == [
        "301.0",
        "3.1.0rc1",
        "3.1.0",
        "3.0.0",
        "3.0.0",
    ]
    assert [s.major for s in sections] == [301, 3, 3, 3, 3]
    assert sections[0].line == 3


def test_non_release_heading_is_a_boundary_without_a_version() -> None:
    text = "# Changelog\n\n## Unreleased\n\n- x\n\n## v1.0.0 (Jan 01, 2026)\n\n- y\n"
    _, sections = _changelog.split_sections(text)
    assert [s.version for s in sections] == [None, "1.0.0"]
    assert sections[0].major is None
    assert sections[1].bullets == ["- y"]


def test_no_sections() -> None:
    assert _changelog.split_sections("# Changelog\n") == ("# Changelog\n", [])
