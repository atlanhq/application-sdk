"""Unified-diff parsing and comment positioning.

GitHub accepts an inline review comment only on a RIGHT-side line that is
inside a hunk (added or context). A model's line number is a claim, not a
fact, so every comment is re-anchored here before it is posted: to the line
its evidence quote actually sits on, else to the nearest commentable line,
else it is dropped. Position drift is a common failure mode of agents on
review; it is fixed in code, not in the prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class Line:
    kind: str  # "+", "-", " "
    old_no: int | None
    new_no: int | None
    text: str


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    status: str = "modified"  # added | deleted | renamed | modified
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_lines(self) -> set[int]:
        return {
            ln.new_no
            for h in self.hunks
            for ln in h.lines
            if ln.kind == "+" and ln.new_no
        }

    @property
    def commentable_lines(self) -> set[int]:
        return {
            ln.new_no
            for h in self.hunks
            for ln in h.lines
            if ln.kind != "-" and ln.new_no
        }

    def _deletions(self) -> list[tuple[int | None, int | None]]:
        """Each pure deletion (a run of removed lines with no added line beside
        it), as the RIGHT-side lines just before and just after it in its hunk
        (None where there is none). A replacement is left out: its added lines
        already mark the change."""
        out: list[tuple[int | None, int | None]] = []
        for h in self.hunks:
            for i, j in _removed_runs(h):
                prev, nxt = _beside(h, i, j)
                if not _is_added(prev) and not _is_added(nxt):
                    out.append((_new_no(prev), _new_no(nxt)))
        return out

    @property
    def deletion_sites(self) -> set[int]:
        """The RIGHT-side lines that border a pure deletion: the line just
        before and just after it. A finding there is about the change, not
        about unchanged code."""
        return {n for run in self._deletions() for n in run if n}

    @property
    def changed_lines(self) -> set[int]:
        """Added lines, and for each pure deletion the one RIGHT-side line it
        is attributed to: the line before it (inside the same function as the
        removed code), else the line after it."""
        return self.added_lines | {
            n for before, after in self._deletions() if (n := before or after)
        }

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind == "-")

    def right_text(self) -> dict[int, str]:
        return {
            ln.new_no: ln.text
            for h in self.hunks
            for ln in h.lines
            if ln.kind != "-" and ln.new_no
        }

    def render(self, max_lines: int | None = None) -> str:
        """The diff as the model sees it: RIGHT-side line numbers on every
        non-deleted line, so a comment's line is read off the page rather
        than counted."""
        out: list[str] = []
        for h in self.hunks:
            out.append(
                f"@@ -{h.old_start},{h.old_len} +{h.new_start},{h.new_len} @@{h.header}"
            )
            for ln in h.lines:
                num = f"{ln.new_no:>5}" if ln.new_no else "     "
                out.append(f"{num} {ln.kind}{ln.text}")
        if max_lines is not None and len(out) > max_lines:
            omitted = len(out) - max_lines
            out = out[:max_lines] + [f"… [{omitted} diff lines omitted]"]
        return "\n".join(out)


def _removed_runs(h: Hunk) -> list[tuple[int, int]]:
    """(start, end) of each run of consecutive removed lines in a hunk, the end
    exclusive."""
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(h.lines):
        if h.lines[i].kind != "-":
            i += 1
            continue
        j = i
        while j < len(h.lines) and h.lines[j].kind == "-":
            j += 1
        runs.append((i, j))
        i = j
    return runs


def _beside(h: Hunk, i: int, j: int) -> tuple[Line | None, Line | None]:
    """The lines just before and just after `h.lines[i:j]` in its hunk."""
    return (h.lines[i - 1] if i > 0 else None), (
        h.lines[j] if j < len(h.lines) else None
    )


def _is_added(ln: Line | None) -> bool:
    return ln is not None and ln.kind == "+"


def _new_no(ln: Line | None) -> int | None:
    return ln.new_no if ln is not None else None


# Git's C-style quoting of a path that holds a special character: `\"`, `\\`,
# the control escapes, and `\ooo` octal for each byte of a non-ASCII character
# (the default `core.quotePath`).
_C_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}
_OCTAL = re.compile(r"[0-7]{3}")


def _unquote(p: str) -> str:
    """A path as Git wrote it, decoded: a quoted one (`"app/a\\"b.py"`,
    `"caf\\303\\251.py"`) loses its quotes and escapes; any other is unchanged."""
    if len(p) < 2 or p[0] != '"' or p[-1] != '"':
        return p
    s, out, i = p[1:-1], bytearray(), 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            if _OCTAL.match(s, i + 1):
                out.append(int(s[i + 1 : i + 4], 8))
                i += 4
                continue
            if s[i + 1] in _C_ESCAPES:
                out.append(_C_ESCAPES[s[i + 1]])
                i += 2
                continue
        out += s[i].encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _strip_prefix(p: str) -> str:
    p = _unquote(p.strip())
    return p[2:] if p[:2] in ("a/", "b/") else p


def _quoted_end(s: str) -> int:
    """The index of the `"` that closes the quoted string `s` opens, or -1."""
    i = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i
        i += 1
    return -1


def _header_path(rest: str) -> str:
    """The new path in a `diff --git <old> <new>` header (`rest` is what follows
    `diff --git `). Either name is C-quoted when it holds a special character,
    and an unquoted name may hold spaces."""
    if rest.startswith('"'):
        end = _quoted_end(rest)
        if end > 0:
            return _strip_prefix(rest[end + 1 :])
    if rest.endswith('"'):
        start = rest.rfind(' "')
        if start >= 0:
            return _strip_prefix(rest[start + 1 :])
    parts = rest.split(" b/", 1)
    return parts[1] if len(parts) == 2 else _strip_prefix(rest.rsplit(" ", 1)[-1])


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            path = _header_path(raw[len("diff --git ") :])
            cur = FileDiff(path=path)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        m = _HUNK_RE.match(raw)
        if m:
            hunk = Hunk(
                int(m.group(1)),
                int(m.group(2) or 1),
                int(m.group(3)),
                int(m.group(4) or 1),
                m.group(5),
            )
            cur.hunks.append(hunk)
            old_no, new_no = hunk.old_start, hunk.new_start
            continue
        if hunk is None or not raw or raw[0] not in "+- \\":
            if raw.startswith("new file mode"):
                cur.status = "added"
            elif raw.startswith("deleted file mode"):
                cur.status = "deleted"
            elif raw.startswith("rename from "):
                cur.old_path = _unquote(raw[len("rename from ") :])
                cur.status = "renamed"
            elif raw.startswith("rename to "):
                cur.path = _unquote(raw[len("rename to ") :])
            elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
                cur.is_binary = True
            elif raw.startswith("+++ "):
                target = raw[4:]
                if target.strip() != "/dev/null":
                    cur.path = _strip_prefix(target)
            continue
        tag, body = raw[0], raw[1:]
        if tag == "\\":  # "\ No newline at end of file"
            continue
        if tag == "+":
            hunk.lines.append(Line("+", None, new_no, body))
            new_no += 1
        elif tag == "-":
            hunk.lines.append(Line("-", old_no, None, body))
            old_no += 1
        else:
            hunk.lines.append(Line(" ", old_no, new_no, body))
            old_no += 1
            new_no += 1
    return files


def _norm(s: str) -> str:
    return " ".join(s.split())


# How `FileDiff.render` shows a removed line: an empty five-wide number column,
# a space, then the marker. Matched exactly, so a unary minus is never eaten.
_REMOVED_RENDER = "      -"


def snippet_lines(snippet: str) -> list[str]:
    """A quoted snippet as comparable lines: whitespace-normalised, a leading
    diff marker or rendered line number stripped, blanks dropped."""
    out: list[str] = []
    for raw in (snippet or "").splitlines():
        s = raw.rstrip()
        m = re.match(r"^\s*\d+\s[+\- ]", s)  # "  123 +code" as rendered to the model
        if m:
            s = s[m.end() :]
        elif s.startswith(_REMOVED_RENDER):  # "      -code": a removed line, no number
            s = s[len(_REMOVED_RENDER) :]
        elif s[:1] in "+-":
            s = s[1:]
        s = _norm(s)
        if s:
            out.append(s)
    return out


def _find_run(seq: list[tuple[int, str]], needle: list[str]) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    n = len(needle)
    for i in range(len(seq) - n + 1):
        if all(seq[i + k][1] == needle[k] for k in range(n)):
            hits.append((seq[i][0], seq[i + n - 1][0]))
    return hits


def _removed_hits(fd: FileDiff, needle: list[str]) -> list[int]:
    """Where `needle` matches the LEFT side of a hunk (context + removed lines)
    in a run that holds at least one removed line: the RIGHT-side line the
    first removed line maps to, or 0 when the hunk has no RIGHT-side line (a
    whole-file deletion). A replaced line maps to the added line that replaced
    it; a pure deletion to the line before it, else the line after it
    (`FileDiff.changed_lines`)."""
    out: list[int] = []
    for h in fd.hunks:
        runs = _removed_runs(h)
        seq = [
            (i, _norm(ln.text))
            for i, ln in enumerate(h.lines)
            if ln.kind != "+" and _norm(ln.text)
        ]
        for lo, hi in _find_run(seq, needle):
            first = next((i for i in range(lo, hi + 1) if h.lines[i].kind == "-"), -1)
            if first < 0:
                continue  # context only: the RIGHT-side match covers it
            i, j = next((i, j) for i, j in runs if i <= first < j)
            prev, nxt = _beside(h, i, j)
            if _is_added(nxt):
                site = _new_no(nxt)
            elif _is_added(prev):
                site = _new_no(prev)
            else:
                site = _new_no(prev) or _new_no(nxt)
            out.append(site or 0)
    return out


def in_removed_lines(fd: FileDiff, snippet: str) -> bool:
    """Whether `snippet` quotes code this diff removes (a run of the LEFT side
    holding at least one removed line)."""
    needle = snippet_lines(snippet)
    return bool(needle) and bool(_removed_hits(fd, needle))


def anchor(fd: FileDiff, snippet: str) -> tuple[int, int] | None:
    """The RIGHT-side (start, end) the quoted code occupies inside the diff, or None.

    The model never supplies a line number — it quotes the code it means and
    the position is computed here (drift is fixed in code, not by asking
    harder). Matching is a consecutive, whitespace-
    normalised run over each hunk's RIGHT side (context + added lines), and
    over its LEFT side (context + removed lines): removed code is the change
    too, and a quote of it is placed on the added line that replaced it, or
    for a pure deletion on the line the deletion is attributed to
    (`FileDiff.changed_lines`). When the snippet appears more
    than once, an occurrence that touches the change wins (a RIGHT-side run
    over an added line or a deletion's border, then removed code), then the
    first. None when nothing matches or the quote is of a file deleted whole,
    which has no RIGHT side to place it on."""
    needle = snippet_lines(snippet)
    if not needle:
        return None
    changed = fd.added_lines | fd.deletion_sites
    hits: list[tuple[int, int]] = []
    for h in fd.hunks:
        seq = [
            (ln.new_no, _norm(ln.text))
            for ln in h.lines
            if ln.kind != "-" and ln.new_no
        ]
        seq = [(n, t) for n, t in seq if t]
        hits.extend(_find_run(seq, needle))
    touching = [h for h in hits if any(n in changed for n in range(h[0], h[1] + 1))]
    removed = [(n, n) for n in _removed_hits(fd, needle) if n]
    ranked = touching + removed + hits
    return ranked[0] if ranked else None


def locate_in_text(text: str, snippet: str) -> int:
    """The first line at which `snippet` occurs (normalised, consecutively) in a
    whole file, or 0 when it does not."""
    needle = snippet_lines(snippet)
    if not needle:
        return 0
    seq = [(i, _norm(t)) for i, t in enumerate(text.splitlines(), 1)]
    seq = [(n, t) for n, t in seq if t]
    hits = _find_run(seq, needle)
    return hits[0][0] if hits else 0


def snippet_in_text(text: str, snippet: str) -> bool:
    """Whether `snippet` still occurs (normalised, consecutively) in a whole file."""
    return locate_in_text(text, snippet) > 0
