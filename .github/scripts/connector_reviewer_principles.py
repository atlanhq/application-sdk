"""The reader for connector-reviewer principle files.

A principle is one short Markdown file. It states something that must stay true
in a connector app, why, and what to ask of a pull request. The fleet principles
(``FP01`` onwards) live in this repository under
``.github/connector-reviewer/principles/``. An app repo keeps its own (``RP01``
onwards) at the same path in that repo, in the same format, so :func:`load`
reads either directory.

``README.md`` in the principles directory describes the format. This module
uses the standard library only, so the reviewer can import it with the system
Python and nothing installed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: The directory that holds the fleet principles.
FLEET_DIR = Path(__file__).resolve().parents[1] / "connector-reviewer" / "principles"

#: The parts of an app repo a principle can be limited to with ``applies-to``.
AREAS = ("app", "generated", "tests", "contract", "ci")

#: The labelled fields, in the order every file must have them.
LABELS = ("Principle:", "Why:", "Ask:", "Don't flag:", "Severity:")

#: A principle file is at most this many lines, front matter included.
MAX_LINES = 26

#: ``Ask:`` takes one to this many bullets.
MAX_ASK_BULLETS = 3

_ID = re.compile(r"(?:FP|RP)\d{2}")
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_FRONT_MATTER_KEYS = ("id", "applies-to")
_FENCE = "---"
_BULLET = "- "


@dataclass(frozen=True)
class Principle:
    """One principle, as read from its file.

    ``areas`` holds what ``applies-to`` listed, in file order. It is empty when
    the file has no ``applies-to``, which means the principle applies
    everywhere; use :meth:`applies_to` rather than testing membership directly.
    ``ask`` holds the bullets without their ``- `` marker. The other fields are
    single paragraphs with their line breaks joined.
    """

    id: str
    title: str
    areas: tuple[str, ...]
    principle: str
    why: str
    ask: tuple[str, ...]
    dont_flag: str
    severity: str
    path: Path

    def applies_to(self, area: str) -> bool:
        """Whether this principle covers *area* (always true with no ``applies-to``)."""
        return not self.areas or area in self.areas


def load(directory: str | os.PathLike[str]) -> list[Principle]:
    """Read every principle in *directory*, ordered by file name.

    Every ``*.md`` file except ``README.md`` must be a principle named
    ``<id>-<slug>.md`` in lower case (``fp01-undeclared-loss.md``). Raises
    :class:`ValueError` naming the file and the problem for anything else, and
    for a directory that does not exist: a wrong path must not read as "no
    principles".
    """
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"{root}: not a directory")
    found: list[Principle] = []
    seen: dict[str, Path] = {}
    for path in sorted(root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        principle = parse(path)
        _check_file_name(principle)
        if principle.id in seen:
            raise ValueError(
                f"{path}: id {principle.id} is already used by {seen[principle.id].name}"
            )
        seen[principle.id] = path
        found.append(principle)
    return found


def parse(path: str | os.PathLike[str]) -> Principle:
    """Read one principle file. Raises :class:`ValueError` naming the file and the problem."""
    file = Path(path)
    try:
        # Inside the try: a file that is not valid UTF-8 raises UnicodeDecodeError
        # (a ValueError), and that message must name the file like any other.
        lines = file.read_text(encoding="utf-8").splitlines()
        return _parse_lines(lines, file)
    except ValueError as problem:
        raise ValueError(f"{file}: {problem}") from problem


def _parse_lines(lines: list[str], path: Path) -> Principle:
    if len(lines) > MAX_LINES:
        raise ValueError(f"has {len(lines)} lines; the limit is {MAX_LINES}")
    front, rest = _split_front_matter(lines)
    meta = _front_matter(front)
    title, body = _title(rest)
    fields = _fields(body)
    return Principle(
        id=_principle_id(meta),
        title=title,
        areas=_areas(meta),
        principle=_paragraph("Principle:", fields),
        why=_paragraph("Why:", fields),
        ask=_bullets(fields["Ask:"]),
        dont_flag=_paragraph("Don't flag:", fields),
        severity=_paragraph("Severity:", fields),
        path=path,
    )


def _split_front_matter(lines: list[str]) -> tuple[list[str], list[str]]:
    if not lines or lines[0] != _FENCE:
        raise ValueError("must start with a '---' line that opens the front matter")
    if _FENCE not in lines[1:]:
        raise ValueError("front matter is not closed by a second '---' line")
    end = lines.index(_FENCE, 1)
    return lines[1:end], lines[end + 1 :]


def _front_matter(front: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in front:
        key, colon, value = line.partition(":")
        if not colon or key not in _FRONT_MATTER_KEYS:
            raise ValueError(
                f"front matter line {line!r} is not 'id: ...' or 'applies-to: [...]'"
            )
        if key in meta:
            raise ValueError(f"front matter has {key!r} twice")
        meta[key] = value.strip()
    if "id" not in meta:
        raise ValueError("front matter has no 'id'")
    return meta


def _principle_id(meta: dict[str, str]) -> str:
    value = meta["id"]
    if not _ID.fullmatch(value):
        raise ValueError(
            f"id {value!r} is not FP or RP followed by two digits (FP01, RP03)"
        )
    return value


def _areas(meta: dict[str, str]) -> tuple[str, ...]:
    raw = meta.get("applies-to")
    if raw is None:
        return ()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise ValueError("applies-to must be an inline list such as [app, contract]")
    areas = tuple(part.strip() for part in raw[1:-1].split(","))
    unknown = [area for area in areas if area not in AREAS]
    if unknown:
        raise ValueError(
            f"applies-to names {unknown[0]!r}; the areas are {', '.join(AREAS)} "
            "(leave applies-to out when the principle applies everywhere)"
        )
    if len(set(areas)) != len(areas):
        raise ValueError("applies-to names an area twice")
    return areas


def _title(rest: list[str]) -> tuple[str, list[str]]:
    if not rest or not rest[0].startswith("# ") or not rest[0][2:].strip():
        raise ValueError(
            "the line after the front matter must be an H1 title ('# Title')"
        )
    if len(rest) < 2 or rest[1].strip():
        raise ValueError("the title must be followed by a blank line")
    return rest[0][2:].strip(), rest[2:]


def _label_of(line: str) -> str | None:
    return next((label for label in LABELS if line.startswith(label)), None)


def _fields(body: list[str]) -> dict[str, list[str]]:
    """Split the body into the raw lines of each labelled field, checking order."""
    while body and not body[-1].strip():
        body = body[:-1]
    fields: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body:
        label = _label_of(line)
        if label is None:
            _continuation(line, current).append(line.rstrip())
            continue
        expected = LABELS[len(fields)] if len(fields) < len(LABELS) else None
        if label != expected:
            raise ValueError(_order_problem(label, fields, expected))
        current = fields[label] = [line[len(label) :].strip()]
    if len(fields) < len(LABELS):
        raise ValueError(
            f"{LABELS[len(fields)]!r} is missing; the fields are "
            f"{', '.join(LABELS)} in that order"
        )
    return fields


def _continuation(line: str, current: list[str] | None) -> list[str]:
    """The field a non-label line continues, or the reason it continues none."""
    if not line.strip():
        raise ValueError(
            "blank line among the fields; each field is one paragraph and the "
            "fields follow one another directly"
        )
    if current is None:
        raise ValueError(f"{line!r} comes before {LABELS[0]!r}")
    return current


def _order_problem(
    label: str, fields: dict[str, list[str]], expected: str | None
) -> str:
    if label in fields:
        return f"{label!r} appears twice"
    return f"{label!r} is out of order; {expected!r} comes first"


def _paragraph(label: str, fields: dict[str, list[str]]) -> str:
    parts = [part.strip() for part in fields[label] if part.strip()]
    if any(part.startswith(_BULLET) for part in parts):
        raise ValueError(f"{label!r} is one paragraph; only 'Ask:' takes '- ' bullets")
    if not parts:
        raise ValueError(f"{label!r} is empty")
    return " ".join(parts)


def _bullets(raw: list[str]) -> tuple[str, ...]:
    head, *items = raw
    if head:
        raise ValueError("'Ask:' must be alone on its line, with '- ' bullets under it")
    for item in items:
        if not item.startswith(_BULLET) or not item[len(_BULLET) :].strip():
            raise ValueError(
                f"under 'Ask:' every line is one '- ' bullet; found {item!r}"
            )
    if not 1 <= len(items) <= MAX_ASK_BULLETS:
        raise ValueError(
            f"'Ask:' has {len(items)} bullets; it takes 1 to {MAX_ASK_BULLETS}"
        )
    return tuple(item[len(_BULLET) :].strip() for item in items)


def _check_file_name(principle: Principle) -> None:
    stem = principle.id.lower()
    if not re.fullmatch(rf"{stem}-{_SLUG}\.md", principle.path.name):
        raise ValueError(
            f"{principle.path}: file name must be '{stem}-<slug>.md' "
            f"(lower case, words joined by '-') to match id {principle.id}"
        )
