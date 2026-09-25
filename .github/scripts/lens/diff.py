"""Unified-diff parsing and comment positioning.

GitHub accepts an inline review comment only on a RIGHT-side line that is
inside a hunk (added or context). A model's line number is a claim, not a
fact, so every comment is re-anchored here before it is posted: to the line
its evidence quote actually sits on, else to the nearest commentable line,
else it is dropped. Position drift is one of the failure modes general agents
show on review (open-code-review lists it first); it is fixed in code, not in
the prompt.
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


def snippet_lines(snippet: str) -> list[str]:
    """A quoted snippet as comparable lines: whitespace-normalised, a leading
    diff marker or rendered line number stripped, blanks dropped."""
    out: list[str] = []
    for raw in (snippet or "").splitlines():
        s = raw.rstrip()
        m = re.match(r"^\s*\d+\s[+\- ]", s)  # "  123 +code" as rendered to the model
        if m:
            s = s[m.end() :]
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


def anchor(fd: FileDiff, snippet: str) -> tuple[int, int] | None:
    """The RIGHT-side (start, end) the quoted code occupies inside the diff, or None.

    The model never supplies a line number — it quotes the code it means and
    the position is computed here (open-code-review's design; drift is fixed
    in code, not by asking harder). Matching is a consecutive, whitespace-
    normalised run over each hunk's RIGHT side (context + added lines). When
    the snippet appears more than once, the occurrence that touches an added
    line wins, then the first."""
    needle = snippet_lines(snippet)
    if not needle:
        return None
    added = fd.added_lines
    hits: list[tuple[int, int]] = []
    for h in fd.hunks:
        seq = [
            (ln.new_no, _norm(ln.text))
            for ln in h.lines
            if ln.kind != "-" and ln.new_no
        ]
        seq = [(n, t) for n, t in seq if t]
        hits.extend(_find_run(seq, needle))
    if not hits:
        return None
    touching = [h for h in hits if any(n in added for n in range(h[0], h[1] + 1))]
    return (touching or hits)[0]


def snippet_in_text(text: str, snippet: str) -> bool:
    """Whether `snippet` still occurs (normalised, consecutively) in a whole file."""
    needle = snippet_lines(snippet)
    if not needle:
        return False
    seq = [(i, _norm(t)) for i, t in enumerate(text.splitlines(), 1)]
    seq = [(n, t) for n, t in seq if t]
    return bool(_find_run(seq, needle))
