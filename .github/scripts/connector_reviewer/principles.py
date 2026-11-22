"""Principles: what the reviewer judges by, matched to files by area.

This replaces lens's rule cards (`rules.toml` and `cards/`). A principle is one
short Markdown file:

    ---
    id: FP01
    applies-to: [app, contract]
    ---
    # Never publish undeclared loss

    Principle: <one or two sentences>
    Why: <one sentence>
    Ask:
    - <question>
    - <at most three bullets>
    Don't flag: <what CI already enforces, citing conformance rule ids in backticks>
    Severity: <what makes it high; otherwise medium>

Two sources, one format:

- **fleet principles** (`FP01`…) hold for every connector app. They are read
  from the directory given by `--principles-dir` or the
  `CONNECTOR_REVIEWER_PRINCIPLES_DIR` environment variable;
- **repo principles** (`RP01`…) are one repository's own. They are read from
  `<root>/.github/connector-reviewer/principles/` on the base branch of the
  repo under review, when that directory exists.

Only a file named `^[a-z]{2}\\d{2}-.+\\.md$` is a principle; a `README.md`
beside them is not read. `applies-to` names areas, which are fixed and coarse
(`area_of`); a principle without it applies to every file. A bundle gets the
principles whose areas meet the areas of its files, fleet first and then repo,
each in id order and each rendered once.

A file that does not follow the format never stops a review: it is skipped and
a note says so in the summary. A fleet directory that is missing, or holds no
principle file at all, is a configuration error and stops the run before any
model call (`PrinciplesError`).

Standard library only, like the rest of the package: the front matter is two
keys, so it is parsed here and not with a YAML library.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

AREAS = ("app", "generated", "tests", "contract", "ci")
ENV_DIR = "CONNECTOR_REVIEWER_PRINCIPLES_DIR"
REPO_DIR = ".github/connector-reviewer/principles"
FILE_NAME = re.compile(r"^[a-z]{2}\d{2}-.+\.md$")
ID_PREFIX = {"fleet": "FP", "repo": "RP"}
MAX_ASKS = 3
# A principle is a few sentences. The cap keeps a stray document dropped in the
# directory out of every prompt.
MAX_FILE_CHARS = 4000

_FRONT_MATTER_KEYS = ("id", "applies-to")
# The five fields, in the order they are rendered. The apostrophe may be typed
# straight or curly.
_FIELDS = ("Principle", "Why", "Ask", "Don't flag", "Severity")
_FIELD_BY_LABEL = {name.lower(): name for name in _FIELDS}
_LABEL = re.compile(r"^(principle|why|ask|don['’]t flag|severity):[ \t]*(.*)$", re.I)
_BULLET = re.compile(r"^[-*][ \t]+(.*)$")
_INLINE_LIST = re.compile(r"^\[(.*)\]$")


def area_of(path: str) -> str | None:
    """The area a path belongs to, or None for a file outside all five.

    The most specific area wins: a file under `app/generated/` is `generated`,
    never `app`."""
    path = path.removeprefix("./")
    if path.startswith("app/generated/"):
        return "generated"
    if path.startswith("app/"):
        return "app"
    if path.startswith("tests/"):
        return "tests"
    if path.startswith("contract/"):
        return "contract"
    if path.startswith(".github/"):
        return "ci"
    return None


class PrinciplesError(Exception):
    """The fleet principles cannot be loaded: a configuration error."""


@dataclass(frozen=True)
class Principle:
    id: str
    title: str
    applies_to: tuple[str, ...]  # areas; empty means every file
    principle: str
    why: str
    ask: tuple[str, ...]
    dont_flag: str
    severity: str
    source: str  # "fleet" | "repo"

    def applies(self, areas: set[str]) -> bool:
        return not self.applies_to or bool(areas.intersection(self.applies_to))

    def render(self, files: list[str]) -> str:
        """The principle as the model sees it: its id and title, the bundle's
        files it applies to, and its five fields."""
        own = ' source="this repository"' if self.source == "repo" else ""
        lines = [
            f'<principle id="{self.id}"{own} title="{_attr(self.title)}" '
            f'for="{_attr(", ".join(files))}">',
            f"Principle: {self.principle}",
            f"Why: {self.why}",
            "Ask:",
            *(f"- {q}" for q in self.ask),
            f"Don't flag: {self.dont_flag}",
            f"Severity: {self.severity}",
            "</principle>",
        ]
        return "\n".join(lines)


@dataclass
class PrincipleSet:
    fleet: list[Principle] = field(default_factory=list)  # in id order
    repo: list[Principle] = field(default_factory=list)  # in id order
    # One line per principle file that was skipped, for the summary.
    notes: list[str] = field(default_factory=list)

    def for_paths(self, paths: list[str]) -> list[Principle]:
        """The principles for a bundle: those whose areas meet the areas of its
        files, and those that apply everywhere. Fleet first, then repo."""
        areas = {a for a in map(area_of, paths) if a}
        return [p for p in (*self.fleet, *self.repo) if p.applies(areas)]

    def render_for(self, paths: list[str]) -> str:
        """Each applicable principle once, with the files it governs."""
        blocks: list[str] = []
        repo_started = False
        for p in self.for_paths(paths):
            if p.source == "repo" and not repo_started:
                repo_started = True
                blocks.append(
                    "The principles below (RP ids) are this repository's own."
                )
            files = [f for f in paths if not p.applies_to or area_of(f) in p.applies_to]
            blocks.append(p.render(files))
        return "\n".join(blocks)


def _attr(value: str) -> str:
    """`value` inside a double-quoted attribute: it cannot close the quote or
    the line. File paths come from the PR."""
    return re.sub(r"[\r\n]+", " ", value).replace('"', "'")


def _shown(value: str, limit: int = 60) -> str:
    """A value from a principle file, safe to show in the Markdown summary."""
    return re.sub(r"[^A-Za-z0-9 ._,'\[\]-]", "?", value)[:limit]


def _front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """The front matter's keys, and the index of the first line after it."""
    if not lines or lines[0].strip() != "---":
        raise ValueError("it does not open with a `---` front-matter line")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("its front matter has no closing `---` line") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in _FRONT_MATTER_KEYS:
            # An unknown key is an error, not ignored: `applies_to:` misspelt
            # would otherwise turn an area principle into an everywhere one.
            raise ValueError(
                f"front-matter line `{_shown(line.strip())}` is not `id:` or `applies-to:`"
            )
        if key in meta:
            raise ValueError(f"front-matter key `{key}` is given twice")
        meta[key] = value.strip()
    return meta, end + 1


def _areas(raw: str) -> tuple[str, ...]:
    m = _INLINE_LIST.match(raw)
    if not m:
        raise ValueError("`applies-to` is not an inline list such as `[app, contract]`")
    items = [x.strip().strip("'\"") for x in m.group(1).split(",")]
    if not all(items):
        raise ValueError("`applies-to` is empty: leave it out to apply everywhere")
    unknown = [x for x in items if x not in AREAS]
    if unknown:
        raise ValueError(
            f"`applies-to` names an unknown area `{_shown(unknown[0])}` "
            f"(the areas are {', '.join(AREAS)})"
        )
    return tuple(dict.fromkeys(items))


def _body(lines: list[str]) -> tuple[str, dict[str, list[str]]]:
    """The title and, per field, its lines (for `Ask`, one entry per bullet)."""
    title = ""
    values: dict[str, list[str]] = {}
    current = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not title:
            if not line.startswith("# ") or not line[2:].strip():
                raise ValueError("its body does not start with a `# Title` line")
            title = line[2:].strip()
            continue
        label = _LABEL.match(line)
        if label:
            current = _FIELD_BY_LABEL[label.group(1).lower().replace("’", "'")]
            if current in values:
                raise ValueError(f"`{current}:` is given twice")
            values[current] = [label.group(2).strip()] if label.group(2).strip() else []
            continue
        if not current:
            raise ValueError("it has text between the title and `Principle:`")
        bullet = _BULLET.match(line) if current == "Ask" else None
        if bullet:
            values[current].append(bullet.group(1).strip())
        elif values[current]:
            values[current][-1] += " " + line  # a wrapped line continues the last one
        else:
            values[current].append(line)
    if not title:
        raise ValueError("it has no `# Title` line")
    return title, values


def parse_principle(text: str, source: str) -> Principle:
    """One principle from the text of its file. Raises ValueError, with the
    reason, for a file that does not follow the format."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    meta, body_start = _front_matter(lines)
    pid = meta.get("id", "").strip("'\"")
    prefix = ID_PREFIX[source]
    if not re.fullmatch(rf"{prefix}\d{{2}}", pid):
        raise ValueError(
            f"its `id` must be {prefix} and two digits for a {source} principle, "
            f"not `{_shown(pid)}`"
        )
    applies_to = _areas(meta["applies-to"]) if "applies-to" in meta else ()
    title, values = _body(lines[body_start:])
    for name in _FIELDS:
        if not values.get(name):
            raise ValueError(f"`{name}:` is missing or empty")
    if len(values["Ask"]) > MAX_ASKS:
        raise ValueError(
            f"`Ask:` has {len(values['Ask'])} questions; at most {MAX_ASKS}"
        )
    return Principle(
        id=pid,
        title=title,
        applies_to=applies_to,
        principle=" ".join(values["Principle"]),
        why=" ".join(values["Why"]),
        ask=tuple(values["Ask"]),
        dont_flag=" ".join(values["Don't flag"]),
        severity=" ".join(values["Severity"]),
        source=source,
    )


def _load_dir(
    directory: Path, source: str, notes: list[str]
) -> tuple[list[Principle], int]:
    """The well-formed principles of one directory, in id order, and how many
    principle-named files it holds. Every skipped file adds a note."""
    found: dict[str, Principle] = {}
    named = 0
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if not FILE_NAME.match(path.name):
            continue  # README.md, and anything else that is not a principle
        named += 1
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("it is not a regular file")
            text = path.read_text(encoding="utf-8")
            if len(text) > MAX_FILE_CHARS:
                raise ValueError(f"it is longer than {MAX_FILE_CHARS} characters")
            p = parse_principle(text, source)
            if not path.name.startswith(p.id.lower() + "-"):
                raise ValueError(f"its `id` {p.id} does not match its file name")
            if p.id in found:
                raise ValueError(f"its `id` {p.id} is already used by another file")
        except UnicodeDecodeError:
            why = "it is not UTF-8 text"
        except OSError:
            why = "it could not be read"
        except ValueError as e:
            why = str(e)
        else:
            found[p.id] = p
            continue
        notes.append(
            f"{source.capitalize()} principle file `{_shown(path.name, 80)}` does not "
            f"follow the principle format and was not used: {why}."
        )
    return [found[k] for k in sorted(found)], named


def resolve_fleet_dir(arg: str | None) -> Path | None:
    """Where the fleet principles are: `--principles-dir` if given, else the
    `CONNECTOR_REVIEWER_PRINCIPLES_DIR` environment variable, else nowhere."""
    chosen = arg or os.environ.get(ENV_DIR) or ""
    return Path(chosen).resolve() if chosen else None


def load_principles(fleet: Path | None, root: Path) -> PrincipleSet:
    """The fleet principles in `fleet`, and the repo principles of the
    base-branch checkout at `root` when it has any.

    Raises PrinciplesError when the fleet directory is not given, does not
    exist, or holds no principle file: the reviewer must not run without the
    principles it judges by. A malformed file in either directory is skipped
    with a note in `PrincipleSet.notes`."""
    if fleet is None:
        raise PrinciplesError(
            f"no fleet principles directory: pass --principles-dir or set {ENV_DIR}"
        )
    if not fleet.is_dir():
        raise PrinciplesError(f"the fleet principles directory does not exist: {fleet}")
    out = PrincipleSet()
    out.fleet, named = _load_dir(fleet, "fleet", out.notes)
    if not named:
        raise PrinciplesError(
            f"the fleet principles directory holds no principle file "
            f"(named like `fp01-<slug>.md`): {fleet}"
        )
    repo = root / REPO_DIR
    if repo.is_symlink():
        out.notes.append(
            f"`{REPO_DIR}` is a symlink, so this repository's principles were not read."
        )
    elif repo.is_dir():
        out.repo, _ = _load_dir(repo, "repo", out.notes)
    return out
