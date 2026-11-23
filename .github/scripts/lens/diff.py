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

    def _removed_runs(self) -> list[tuple[int | None, int | None]]:
        """Each run of consecutive removed lines, as the RIGHT-side lines just
        before and just after it in its hunk (None where there is none)."""
        runs: list[tuple[int | None, int | None]] = []
        for h in self.hunks:
            i = 0
            while i < len(h.lines):
                if h.lines[i].kind != "-":
                    i += 1
                    continue
                j = i
                while j < len(h.lines) and h.lines[j].kind == "-":
                    j += 1
                before = h.lines[i - 1].new_no if i > 0 else None
                after = h.lines[j].new_no if j < len(h.lines) else None
                runs.append((before, after))
                i = j
        return runs

    @property
    def deletion_sites(self) -> set[int]:
        """The RIGHT-side lines that border a deletion: the line just before and
        just after each run of removed lines. A finding there is about the
        change, not about unchanged code."""
        return {n for run in self._removed_runs() for n in run if n}

    @property
    def changed_lines(self) -> set[int]:
        """Added lines, and for each deletion the one RIGHT-side line it is
        attributed to: the line before it (inside the same function as the
        removed code), else the line after it."""
        return self.added_lines | {
            n for before, after in self._removed_runs() if (n := before or after)
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


def _strip_prefix(p: str) -> str:
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            parts = raw[len("diff --git ") :].split(" b/", 1)
            path = (
                parts[1] if len(parts) == 2 else _strip_prefix(raw.rsplit(" ", 1)[-1])
            )
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
                cur.old_path = raw[len("rename from ") :]
                cur.status = "renamed"
            elif raw.startswith("rename to "):
                cur.path = raw[len("rename to ") :]
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
    first removed line's deletion is attributed to (the line before it in the
    hunk, else the line after it), or 0 when the hunk has no RIGHT-side line
    (a whole-file deletion)."""
    out: list[int] = []
    for h in fd.hunks:
        seq = [
            (i, _norm(ln.text))
            for i, ln in enumerate(h.lines)
            if ln.kind != "+" and _norm(ln.text)
        ]
        for lo, hi in _find_run(seq, needle):
            gone = [i for i in range(lo, hi + 1) if h.lines[i].kind == "-"]
            if not gone:
                continue  # context only: the RIGHT-side match covers it
            r = gone[0]
            while r > 0 and h.lines[r - 1].kind == "-":
                r -= 1
            before = next(
                (ln.new_no for ln in reversed(h.lines[:r]) if ln.new_no), None
            )
            after = next((ln.new_no for ln in h.lines[r:] if ln.new_no), None)
            out.append(before or after or 0)
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
    too, and a quote of it is placed on the RIGHT-side line its deletion is
    attributed to (`FileDiff.changed_lines`). When the snippet appears more
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
