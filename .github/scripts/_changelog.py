"""One reader for the ``## v<version> (<date>)`` sections of a CHANGELOG.md.

``extract_release_notes.py`` (the GitHub release body), ``release_bump_debounce.py``
(has this version's notes changed?), ``lint_changelog.py`` and the major-bump
archive in ``update_changelog.py`` all need the same answer to "where does the
section for this version start and end". Each used to carry its own regex, and
they disagreed: the extractor interpolated the version unescaped, so its ``.``
matched any character, and it ended a section at any ``## v`` substring rather
than at the next heading line.

A section runs from its heading line to the next line starting ``## `` (or the
end of the file). Everything before the first heading is the preamble.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `## v3.60.0 (November 26, 2026)`. The version token is everything up to the
# first space, so pre-release forms like `0.1.1rc61` parse too.
_HEADING_RE = re.compile(r"^## v(?P<version>\S+) \((?P<date>[^)\n]*)\)[ \t]*$")
_BOUNDARY_RE = re.compile(r"^## ", re.MULTILINE)


@dataclass(frozen=True)
class Section:
    """One ``## `` section of a changelog, with its exact source text."""

    #: The version from a ``## v<version> (<date>)`` heading; None for any
    #: other ``## `` heading.
    version: str | None
    #: The heading line, without its newline.
    heading: str
    #: The section's full text, heading included, up to (not including) the
    #: next heading. Joining every section's text after the preamble gives
    #: back the file byte for byte.
    text: str
    #: 1-based line number of the heading in the file it was read from.
    line: int

    @property
    def body(self) -> str:
        """The text after the heading line."""
        return self.text.partition("\n")[2]

    @property
    def bullets(self) -> list[str]:
        """The top-level ``- `` bullet lines of the section."""
        return [line for line in self.body.splitlines() if line.startswith("- ")]

    @property
    def major(self) -> int | None:
        """The leading integer of the version, or None when it has none."""
        if self.version is None:
            return None
        match = re.match(r"\d+", self.version)
        return int(match.group(0)) if match else None


def split_sections(text: str) -> tuple[str, list[Section]]:
    """Split a changelog into its preamble and its ``## `` sections, in order."""
    starts = [m.start() for m in _BOUNDARY_RE.finditer(text)]
    if not starts:
        return text, []
    sections: list[Section] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[start:end]
        heading = chunk.partition("\n")[0]
        match = _HEADING_RE.match(heading)
        sections.append(
            Section(
                version=match.group("version") if match else None,
                heading=heading,
                text=chunk,
                line=text.count("\n", 0, start) + 1,
            )
        )
    return text[: starts[0]], sections


def sections_for(text: str, version: str) -> list[Section]:
    """Every section whose heading names exactly ``version``, newest first."""
    version = version.removeprefix("v")
    return [s for s in split_sections(text)[1] if s.version == version]


def find_section(text: str, version: str) -> Section | None:
    """The first (newest) section for ``version``, or None."""
    matches = sections_for(text, version)
    return matches[0] if matches else None
