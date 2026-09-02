"""Assemble the reviewer's first turn, so it does not have to go and find one.

`REVIEW.md` opens by telling the reviewer that everything with a determinate
answer is already in its context: the PR's facts, the diff, the files worth
reading, which specialist it is, and what the deterministic gate found. Until
this module existed that sentence was aspirational — the prompt said "read the
playbook and follow it", and the reviewer spent its first several turns
orienting: listing files, grepping for callers, working out its own scope.

Those turns are the most expensive ones in the review. They happen while the
context is smallest and cheapest, they produce facts the runner already holds or
can compute in milliseconds, and every one of them lands in the transcript that
the *remaining* turns then carry.

## Why the pack is narrow rather than large

The obvious move once you own the context is to put everything in it. That is
measurably wrong. Retrieval-quality work on review agents finds models degrade
with excess context through attention dilution — a reviewer handed the whole
module reliably comments on the parts of it the PR did not touch. So the pack is
deliberately bounded:

* the diff, which is the subject
* the symbols the diff actually changed, resolved by parsing the file rather
  than by pattern-matching the patch
* who calls those symbols, capped, because reachability decides severity: the
  same defect is a nit on a private helper and a blocker on something every
  connector executes
* the tests that already exist next to the change, so "untested" is a claim the
  reviewer can check rather than assume
* what the deterministic gate already decided, so the review does not spend a
  round restating a blocking CI finding

Caps are constants here rather than parameters. A cap that callers can raise
gets raised, and then the pack is large again by increments nobody reviewed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

#: Symbols carried into the pack. Past this the diff is broad enough that the
#: per-module split is the right answer, not a longer list.
MAX_SYMBOLS = 40

#: Callers listed per symbol. Enough to establish blast radius; a full list is
#: a search result, and the reviewer does not need to read one to size a
#: finding.
MAX_CALLERS_PER_SYMBOL = 6

#: Existing test files surfaced alongside the change.
MAX_NEARBY_TESTS = 12

#: Files scanned when resolving callers. The SDK is small enough that this is
#: milliseconds; the bound exists so a runaway repo cannot stall a review.
MAX_SCANNED_FILES = 4000

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NEW_FILE = re.compile(r"^\+\+\+ b/(.+)$")
_OLD_FILE = re.compile(r"^--- a/(.+)$")

_TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.py$")


@dataclass(frozen=True)
class ChangedFile:
    path: str
    added: tuple[int, ...]
    removed_count: int
    is_deleted: bool

    @property
    def is_test(self) -> bool:
        return bool(_TEST_PATH.search(self.path))

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")


@dataclass(frozen=True)
class Symbol:
    """A function or class the diff changed, with the lines it changed in it."""

    name: str
    qualname: str
    kind: str
    path: str
    lineno: int
    end_lineno: int


@dataclass
class Pack:
    scope: str
    mode: str
    agents: tuple[str, ...]
    files: tuple[ChangedFile, ...]
    symbols: tuple[Symbol, ...] = ()
    callers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    nearby_tests: tuple[str, ...] = ()
    gate: str = ""
    truncated: tuple[str, ...] = ()

    @property
    def changed_lines(self) -> int:
        return sum(len(f.added) + f.removed_count for f in self.files)

    @property
    def touches_config(self) -> bool:
        return any(
            f.path.startswith((".github/", "helm/"))
            or f.path in ("pyproject.toml", "uv.lock")
            for f in self.files
        )

    @property
    def touches_conformance(self) -> bool:
        return any(
            f.path.startswith(("packages/conformance/", "remediation/"))
            for f in self.files
        )


# --------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------


def parse_diff(text: str) -> tuple[ChangedFile, ...]:
    """Changed files and the line numbers each one adds.

    Line numbers are taken from the hunk headers rather than counted, because a
    counted offset drifts silently on a malformed or truncated patch and the
    reviewer then gets symbol attributions that are subtly wrong — worse than
    none, since it will quote the wrong code as evidence.
    """
    out: list[ChangedFile] = []
    path: str | None = None
    old_path: str | None = None
    added: list[int] = []
    removed = 0
    deleted = False
    cursor = 0

    def flush() -> None:
        nonlocal path, old_path, added, removed, deleted
        # A deleted file has no `+++ b/…` line at all — its identity lives on
        # the `---` side. Dropping it would hide the shape a removed public
        # export makes in a diff, which is exactly the change most worth review.
        resolved = path or (old_path if deleted else None)
        if resolved is not None:
            out.append(
                ChangedFile(
                    path=resolved,
                    added=tuple(added),
                    removed_count=removed,
                    is_deleted=deleted,
                )
            )
        path, old_path, added, removed, deleted = None, None, [], 0, False

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            continue
        old = _OLD_FILE.match(line)
        if old:
            old_path = old.group(1)
            continue
        new = _NEW_FILE.match(line)
        if new:
            path = new.group(1)
            deleted = path == "/dev/null"
            continue
        if line.startswith("+++ /dev/null"):
            deleted = True
            continue
        hunk = _HUNK.match(line)
        if hunk:
            cursor = int(hunk.group(1))
            continue
        # `not deleted` matters: a deleted file never gets a `+++ b/…` line, so
        # `path` stays None while its removals are still real changed lines. The
        # obvious `if path is None: continue` silently counts a 900-line
        # deletion as zero and hands it the single-pass depth.
        if path is None and not deleted:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(cursor)
            cursor += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        elif line.startswith(" "):
            cursor += 1
    flush()
    return tuple(f for f in out if f.path != "/dev/null")


# --------------------------------------------------------------------------
# What the diff actually changed
# --------------------------------------------------------------------------


def touched_symbols(repo: Path, files: Iterable[ChangedFile]) -> tuple[Symbol, ...]:
    """Functions and classes containing at least one added line.

    Resolved by parsing the file rather than by reading `@@` headers. Git's
    hunk-header function hint is a heuristic — it reports the nearest preceding
    `def` textually, which is the wrong answer for nested functions, decorators
    and methods, and the reviewer would inherit that error as a fact.

    A file that does not parse is skipped rather than guessed at. It is either
    not Python or it is syntactically broken, and in the second case the review
    has a much louder finding available to it than a symbol list.
    """
    found: list[Symbol] = []
    for changed in files:
        if changed.is_deleted or not changed.is_python or not changed.added:
            continue
        source_path = repo / changed.path
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        added = set(changed.added)
        for node, qualname in _walk_defs(tree):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            if any(node.lineno <= line <= end for line in added):
                found.append(
                    Symbol(
                        name=node.name,
                        qualname=qualname,
                        kind=_kind(node),
                        path=changed.path,
                        lineno=node.lineno,
                        end_lineno=end,
                    )
                )
    # Innermost first: a method is more specific than the class holding it, and
    # when the cap bites the specific ones are the ones worth keeping.
    found.sort(key=lambda s: (-s.qualname.count("."), s.path, s.lineno))
    return tuple(found[:MAX_SYMBOLS])


def _kind(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    return "function"


def _walk_defs(tree: ast.AST, prefix: str = "") -> Iterable[tuple[ast.AST, str]]:
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{prefix}{node.name}"
            yield node, qualname
            yield from _walk_defs(node, prefix=f"{qualname}.")


# --------------------------------------------------------------------------
# Who calls it — the input severity actually depends on
# --------------------------------------------------------------------------


def find_callers(
    repo: Path, symbols: Sequence[Symbol], *, scan_root: str = "."
) -> dict[str, tuple[str, ...]]:
    """Files referencing each changed symbol, excluding where it is defined.

    Reachability is the input that decides how much a finding is worth: the same
    defect is a nit on a private helper and a blocker on something every
    connector executes. The reviewer would otherwise establish this by grepping,
    which costs a turn and is the single most common orientation step in the
    measured transcripts.

    Name-based and therefore approximate — a same-named symbol elsewhere counts.
    That is stated in the rendered pack rather than hidden, because a reviewer
    told "approximate" will verify a blocker and accept a nit, which is the
    right allocation of effort.
    """
    if not symbols:
        return {}
    wanted = {s.name for s in symbols}
    defining = {s.path for s in symbols}
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in sorted(wanted)) + r")\b"
    )

    hits: dict[str, list[str]] = {name: [] for name in wanted}
    scanned = 0
    for path in sorted((repo / scan_root).rglob("*.py")):
        scanned += 1
        if scanned > MAX_SCANNED_FILES:
            break
        rel = str(path.relative_to(repo))
        if rel in defining or "/.venv/" in f"/{rel}" or rel.startswith(".venv/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in set(pattern.findall(text)):
            bucket = hits[match]
            if rel not in bucket and len(bucket) < MAX_CALLERS_PER_SYMBOL:
                bucket.append(rel)
    return {name: tuple(paths) for name, paths in hits.items() if paths}


def nearby_tests(repo: Path, files: Iterable[ChangedFile]) -> tuple[str, ...]:
    """Test files that already exercise the changed modules.

    So that "this is untested" becomes a claim the reviewer can check instead of
    an assumption it can make for free. An unfounded missing-tests finding is
    among the most expensive false positives available: it is plausible, it is
    tedious to refute, and refuting it is the author's job.
    """
    modules = {
        Path(f.path).stem
        for f in files
        if f.is_python and not f.is_test and not f.is_deleted
    }
    if not modules:
        return ()
    out: list[str] = []
    for path in (
        sorted((repo / "tests").rglob("test_*.py")) if (repo / "tests").exists() else []
    ):
        rel = str(path.relative_to(repo))
        if Path(rel).stem.removeprefix("test_") in modules:
            out.append(rel)
        elif any(m in rel for m in modules):
            out.append(rel)
        if len(out) >= MAX_NEARBY_TESTS:
            break
    return tuple(out)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_pack(
    *,
    repo: Path,
    diff: str,
    scope: str,
    routing,
    gate: str = "",
) -> Pack:
    """Everything the reviewer needs, and deliberately nothing else."""
    files = parse_diff(diff)
    pack = Pack(scope=scope, mode="", agents=(), files=files, gate=gate)
    pack.mode = routing.mode_for(pack.changed_lines)
    pack.agents = routing.route(scope).resolve(
        touches_config=pack.touches_config,
        touches_conformance=pack.touches_conformance,
    )
    pack.symbols = touched_symbols(repo, files)
    pack.callers = find_callers(repo, pack.symbols)
    pack.nearby_tests = nearby_tests(repo, files)

    truncated: list[str] = []
    if len(pack.symbols) == MAX_SYMBOLS:
        truncated.append(f"symbol list capped at {MAX_SYMBOLS}")
    if len(pack.nearby_tests) == MAX_NEARBY_TESTS:
        truncated.append(f"nearby tests capped at {MAX_NEARBY_TESTS}")
    pack.truncated = tuple(truncated)
    return pack


def render(pack: Pack, agent: str) -> str:
    """The reviewer's first turn, as text.

    Every cap that bit is stated. A pack that silently truncated would let the
    reviewer conclude a symbol has no callers when the list was simply cut,
    which turns a blocker into a nit for a reason nobody can see afterwards.
    """
    lines = [
        f"You are the {agent} specialist.",
        "",
        f"Scope: {pack.scope} · review mode: {pack.mode} · "
        f"{len(pack.files)} files, {pack.changed_lines} changed lines",
    ]
    if pack.mode == "per_module":
        lines.append(
            "This diff is past the size where a single pass finds things "
            "reliably; you are reviewing one module of it."
        )
    lines += ["", "## Files in this change", ""]
    for f in pack.files:
        mark = " (deleted)" if f.is_deleted else " (test)" if f.is_test else ""
        lines.append(f"- {f.path}{mark} · +{len(f.added)}/-{f.removed_count}")

    if pack.symbols:
        lines += ["", "## What this diff changed", ""]
        for s in pack.symbols:
            callers = pack.callers.get(s.name, ())
            where = (
                f" — referenced in {', '.join(callers)}"
                if callers
                else " — no references found in this repo"
            )
            lines.append(f"- {s.kind} `{s.qualname}` ({s.path}:{s.lineno}){where}")
        lines.append("")
        lines.append(
            "References are resolved by name, so a same-named symbol elsewhere "
            "may appear. Verify before resting a blocking finding on one."
        )

    if pack.nearby_tests:
        lines += ["", "## Tests that already cover these modules", ""]
        lines += [f"- {t}" for t in pack.nearby_tests]
        lines.append("")
        lines.append("Check these before reporting anything as untested.")

    if pack.gate:
        lines += ["", "## Already blocked by CI — do not restate", "", pack.gate]

    if pack.truncated:
        lines += ["", "## Truncated", ""]
        lines += [f"- {t}" for t in pack.truncated]

    return "\n".join(lines)
