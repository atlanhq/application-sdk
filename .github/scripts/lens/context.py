"""The context a reviewer needs, assembled in code before the first turn.

An audit of lens's first real review (#3987) found the model had been given
10 of the 64 lines of the function it was reviewing, none of the code the PR
said it was copying, callers that were only thin wrappers, a test list cut to
the wrong four files, and no hint that a public API was changing. Each builder
here closes one of those gaps, deterministically and within a token budget:

- ``changed_functions``   the whole changed function, changed lines marked;
- ``referenced_patterns`` repo symbols the change or the PR text names;
- ``call_sites``          real call sites, followed through thin wrappers;
- ``ranked_tests``        the tests most likely to matter, in order;
- ``public_api``          which changed behaviour is public, and its reach.

Nothing here calls a model; it reads the index and the PR-head text only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .diff import FileDiff
from .index import Symbol
from .tools import Workspace

FN_MAX_LINES = 150  # per changed function
BUNDLE_MAX_FN_LINES = 450  # all changed functions in one bundle
PATTERN_MAX = 3
PATTERN_BODY_LINES = 40
CALL_SITES_MAX = 6
TESTS_MAX = 6
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
_COMMON = {
    "self",
    "None",
    "True",
    "False",
    "return",
    "async",
    "await",
    "kwargs",
    "args",
    "value",
    "result",
    "logger",
    "message",
    "items",
    "data",
    "name",
    "path",
    "list",
    "dict",
    "string",
}


def _is_test(path: str) -> bool:
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or path.rsplit("/", 1)[-1].startswith("test_")
    )


def _lines(ws: Workspace, path: str) -> list[str]:
    return (ws.text(path) or "").splitlines()


def changed_symbols(ws: Workspace, fd: FileDiff) -> list[Symbol]:
    """Innermost symbols enclosing an added line, in file order, once each."""
    seen: dict[str, Symbol] = {}
    for line in sorted(fd.added_lines):
        s = ws.index.enclosing(fd.path, line)
        if s and s.qualname not in seen:
            seen[s.qualname] = s
    return list(seen.values())


# ---- 1. the whole changed function -----------------------------------------------


def changed_functions(ws: Workspace, files: list[FileDiff]) -> str:
    """Each changed function's full PR-head body, changed lines marked `+`.

    The diff shows a few lines around each hunk; a fix can only be checked
    for completeness against the whole function. Source files come before
    tests; a function over `FN_MAX_LINES` is windowed around its changes.
    """
    blocks: list[str] = []
    budget = BUNDLE_MAX_FN_LINES
    ordered = sorted(files, key=lambda f: (_is_test(f.path), f.path))
    for fd in ordered:
        if not fd.path.endswith(".py") or budget <= 0:
            continue
        lines = _lines(ws, fd.path)
        added = fd.added_lines
        for s in changed_symbols(ws, fd):
            lo, hi = s.start, min(s.end, len(lines))
            if s.kind == "class" and hi - lo > FN_MAX_LINES:
                continue  # a change directly in a big class body: the diff hunk is the context
            if hi - lo + 1 > FN_MAX_LINES:
                ch = [n for n in added if lo <= n <= hi] or [lo]
                lo, hi = max(lo, min(ch) - 40), min(hi, max(ch) + 40)
            take = min(hi - lo + 1, budget)
            if take <= 0:
                break
            body = "\n".join(
                f"{n:>5} {'+' if n in added else ' '} {lines[n - 1]}"
                for n in range(lo, lo + take)
            )
            budget -= take
            blocks.append(
                f'<function path="{fd.path}" lines="{lo}-{lo + take - 1}" name="{s.qualname.split(":")[-1]}">\n{body}\n</function>'
            )
    return "\n".join(blocks)


# ---- 3. repo symbols the change or the PR text names -----------------------------


def referenced_patterns(ws: Workspace, files: list[FileDiff], pr_text: str) -> str:
    """Up to `PATTERN_MAX` repo symbols the change uses or the PR text names.

    "This matches the existing AtlanLoggerAdapter pattern" is a claim a
    reviewer can only check with that code in view. Names in the PR text
    weigh most; ambiguous names (defined in several places) and anything
    defined in the files under review are skipped."""
    own = {fd.path for fd in files}
    # Files the PR text names ("… what logger_adaptor.py already does") are the
    # strongest hint: a name defined in several places resolves to the one there,
    # and a named file with no named symbol contributes its main public class.
    # A mention with a directory ("application_sdk/app/context.py") names that exact file;
    # a bare filename ("logger_adaptor.py") names any file with that name. Files under
    # review never count — the PR naming its own file is not a pointer elsewhere.
    own_stems = {p.rsplit("/", 1)[-1][:-3].lower() for p in own}
    named_paths: set[str] = set()
    named_stems: set[str] = set()
    for m in re.findall(r"([A-Za-z0-9_./-]*[A-Za-z0-9_])\.py\b", pr_text or ""):
        if "/" in m:
            named_paths.add(m.lstrip("./") + ".py")
        elif m.lower() not in own_stems:
            named_stems.add(m.lower())

    def in_named_file(d: Symbol) -> bool:
        if d.path in own:
            return False
        return (
            d.path in named_paths
            or d.path.rsplit("/", 1)[-1][:-3].lower() in named_stems
        )

    named_files = named_stems  # used below for the main-class fallback

    # What counts as a reference is CODE, not prose: a plain English word that happens
    # to name a function ("activity", "records") must not crowd out a real one.
    score: dict[str, int] = {}
    for m in re.findall(
        r"`([^`\n]+)`", pr_text or ""
    ):  # names in backticks: strongest…
        # …but only as a symbol: a lone name, a call (`get_logger()`), or a CamelCase
        # class — never a keyword argument (`records=3`) or a value.
        m = m.strip()
        cands = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]{3,})\s*\(", m)) | set(
            re.findall(r"\b[A-Z][a-z]+[A-Z]\w*", m)
        )
        if _IDENT.fullmatch(m):
            cands.add(m)
        for name in cands:
            score[name] = score.get(name, 0) + 5
    for m in _IDENT.findall(pr_text or ""):
        if "_" in m.strip("_") or re.search(
            r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", m
        ):  # snake/CamelCase
            score[m] = score.get(m, 0) + 2
    for fd in files:
        for h in fd.hunks:
            for ln in h.lines:
                if ln.kind == "+":
                    for m in re.findall(
                        r"(?:\.|\b)([A-Za-z_][A-Za-z0-9_]{3,})\s*\(", ln.text
                    ):  # calls
                        score[m] = score.get(m, 0) + 1
    picks: list[Symbol] = []
    # A file the PR text names contributes its main public class first.
    for path, quals in ws.index.by_path.items():
        if (
            path in own
            or _is_test(path)
            or not (
                path in named_paths
                or path.rsplit("/", 1)[-1][:-3].lower() in named_files
            )
        ):
            continue
        classes = [
            ws.index.symbols[q]
            for q in quals
            if ws.index.symbols[q].kind == "class"
            and not ws.index.symbols[q].name.startswith("_")
            and "." not in q.split(":")[-1]
        ]
        classes.sort(key=lambda c: -(c.end - c.start))
        if classes and len(picks) < PATTERN_MAX:
            picks.append(classes[0])
    for name, _ in sorted(score.items(), key=lambda kv: -kv[1]):
        if name in _COMMON:
            continue
        defs = [
            d
            for d in ws.index.definitions(name, limit=8)
            if d.path not in own and not _is_test(d.path)
        ]
        hinted = [d for d in defs if in_named_file(d)]
        pick = hinted[0] if len(hinted) == 1 else (defs[0] if len(defs) == 1 else None)
        # Skip anything already shown, including a method of a class already picked.
        if pick and all(
            pick.qualname != p.qualname
            and not pick.qualname.startswith(p.qualname + ".")
            for p in picks
        ):
            picks.append(pick)
        if len(picks) >= PATTERN_MAX:
            break
    out: list[str] = []
    for d in picks:
        lines = _lines(ws, d.path)
        if d.kind == "class":
            methods = [
                ws.index.symbols[q]
                for q in ws.index.by_path.get(d.path, [])
                if q.startswith(d.qualname + ".")
            ]
            body = "\n".join(f"    {m.signature}" for m in methods[:15])
        else:
            body = "\n".join(
                lines[d.start - 1 : min(d.end, d.start + PATTERN_BODY_LINES - 1)]
            )
        out.append(
            f'<pattern name="{d.name}" path="{d.path}" lines="{d.start}-{d.end}">\n{d.signature}'
            + (f"  — {d.doc}" if d.doc else "")
            + f"\n{body}\n</pattern>"
        )
    return "\n".join(out)


# ---- 4. real call sites, through thin wrappers -----------------------------------


def _is_thin_wrapper(c: Symbol, target: str) -> bool:
    return c.kind != "class" and (c.end - c.start) <= 6 and target in c.calls


def _call_line(ws: Workspace, caller: Symbol, name: str) -> tuple[int, str]:
    lines = _lines(ws, caller.path)
    for n in range(caller.start, min(caller.end, len(lines)) + 1):
        if re.search(rf"\b{re.escape(name)}\s*\(", lines[n - 1]):
            return n, lines[n - 1].strip()
    return caller.start, caller.signature


@dataclass
class Reach:
    """Who calls a changed symbol — directly or through thin wrappers."""

    via: list[str] = field(default_factory=list)  # wrapper names the calls go through
    sites: list[tuple[str, int, str]] = field(
        default_factory=list
    )  # (path, line, code)
    total: int = 0
    approximate: bool = False  # counted through shared wrapper names (bare-name match)
    likely: int = 0  # of `total`, the calls in files that import the changed module

    def count(self) -> str:
        """A count the model can quote without inflating the blast radius. Through a
        shared name (`info`, `error`) every logger in the repo matches, so the bare-name
        total is NOT this code's reach: only calls in files importing the module are."""
        if not self.approximate:
            return str(self.total)
        names = "/".join(self.via)
        return (
            f"{self.likely} likely (in files that import this module); the other "
            f"{self.total - self.likely} calls only share the name(s) {names} with "
            "unrelated code and are not this code's callers"
        )


_IMPORTERS: dict[tuple[int, str], set[str]] = {}


def _importers(ws: Workspace, module: str) -> set[str]:
    """Files that import `module` (by text), computed once per run and module."""
    key = (id(ws), module)
    if key not in _IMPORTERS:
        parent, _, leaf = module.rpartition(".")
        pats = (
            f"import {module}",
            f"from {module} import",
            f"from {parent} import {leaf}",
        )
        _IMPORTERS[key] = {
            p
            for p in ws.files()
            if p.endswith(".py") and any(x in (ws.text(p) or "") for x in pats)
        }
    return _IMPORTERS[key]


def call_sites(ws: Workspace, s: Symbol) -> Reach:
    """Call sites of `s`, following same-file thin wrappers one hop.

    A private `_log` is only ever called by `info`/`warning`/`error`; the
    contract a change breaks lives at THEIR callers. Sites outside tests and
    outside the symbol's own file come first."""
    reach = Reach()
    direct = [
        ws.index.symbols[q]
        for q in ws.index.callers.get(s.name, [])
        if q in ws.index.symbols and q != s.qualname
    ]
    targets: list[tuple[Symbol, str]] = []
    for c in direct:
        if c.path == s.path and _is_thin_wrapper(c, s.name):
            reach.via.append(c.name)
            for q in ws.index.callers.get(c.name, []):
                cc = ws.index.symbols.get(q)
                if cc and cc.path != s.path:
                    targets.append((cc, c.name))
        else:
            targets.append((c, s.name))
    reach.total = len(targets)
    # Wrapper names like info/warning are shared by every logger, so the count through
    # them is a bare-name over-approximation; call sites in files that import the
    # changed module are the likely-real ones and are listed first.
    reach.approximate = bool(reach.via)
    module = s.path[:-3].replace("/", ".")
    importers = _importers(ws, module)
    reach.likely = sum(1 for c, _ in targets if c.path in importers or c.path == s.path)
    targets.sort(
        key=lambda t: (
            t[0].path not in importers,
            _is_test(t[0].path),
            t[0].path == s.path,
            t[0].path,
        )
    )
    # Through shared wrapper names, keep sites whose receiver looks like the changed
    # class (a `_WorkflowSafeLogger` is called as `…log….info(`, not `workflow.info(`),
    # and never a docstring or comment line. If nothing survives, show the raw sites.
    cls = (
        s.qualname.split(":")[-1].split(".")[0]
        if "." in s.qualname.split(":")[-1]
        else ""
    )
    nouns = re.findall(r"[A-Z][a-z]+", cls)
    stem = nouns[-1][:3].lower() if nouns else ""
    picked: list[tuple[str, int, str]] = []
    fallback: list[tuple[str, int, str]] = []
    for c, name in targets:
        if len(picked) >= CALL_SITES_MAX:
            break
        n, code = _call_line(ws, c, name)
        site = (c.path, n, code[:160])
        if len(fallback) < CALL_SITES_MAX:
            fallback.append(site)
        looks_real = not code.startswith(("#", '"', "'")) and "``" not in code
        if reach.approximate and stem:
            looks_real = (
                looks_real
                and re.search(rf"\w*{stem}\w*\.{re.escape(name)}\s*\(", code, re.I)
                is not None
            )
        if looks_real:
            picked.append(site)
    reach.sites = picked or fallback
    return reach


# ---- 5. tests, most relevant first ------------------------------------------------


def ranked_tests(ws: Workspace, fd: FileDiff, names: list[str]) -> list[str]:
    """Tests for `fd`: changed in this PR, then unit, then naming a changed
    symbol, then integration — so the list is never cut to the wrong four."""
    found = list(ws.index.tests_for.get(fd.path, []))
    changed = [p for p in ws.diffs if _is_test(p)]
    for p in changed:
        text = ws.text(p) or ""
        if p not in found and any(n in text for n in names):
            found.append(p)

    def rank(p: str) -> tuple[int, int, int, str]:
        text = ws.text(p) or "" if p in changed else ""
        return (
            p not in changed,
            "/unit/" not in p,
            not any(n in text for n in names),
            p,
        )

    return sorted(set(found), key=rank)[:TESTS_MAX]


# ---- 6. public API ------------------------------------------------------------------


def _public(s: Symbol) -> bool:
    if not s.path.startswith("application_sdk/"):
        return False
    module_private = any(
        part.startswith("_") and part != "__init__.py" for part in s.path.split("/")
    )
    return not module_private and not any(
        p.startswith("_") for p in s.qualname.split(":")[-1].split(".")
    )


def public_api(ws: Workspace, s: Symbol, reach: Reach) -> str:
    """One line when a changed symbol is part of the public API, directly or
    through the public wrappers it is reached by — so the approach check must
    weigh the blast radius, and must say what it could not see."""
    if _public(s):
        exposed = s.qualname.split(":")[-1]
    else:
        wrappers = [w for w in reach.via if not w.startswith("_")]
        if (
            not wrappers
            or s.path.startswith("tests/")
            or not s.path.startswith("application_sdk/")
        ):
            return ""
        exposed = "/".join(wrappers) + f" (via {s.name})"
    return (
        f"PUBLIC API behaviour change: {exposed} — call sites in this repo: {reach.count()}. "
        "Consumers in other repositories were NOT checked."
    )


# ---- the <context> block --------------------------------------------------------------


def build(ws: Workspace, files: list[FileDiff], pr_text: str) -> tuple[str, list[str]]:
    """The context block for a bundle, and its public-API lines (for the approach check)."""
    out: list[str] = []
    api_lines: list[str] = []
    for fd in files:
        if not fd.path.endswith(".py"):
            continue
        syms = changed_symbols(ws, fd)
        names = [s.name for s in syms]
        tests = ranked_tests(ws, fd, names)
        out.append(
            f"{fd.path} ({fd.status}, +{fd.additions}/-{fd.deletions}); tests: "
            + (", ".join(tests) if tests else "NONE import this module")
        )
        for s in syms[:8]:
            reach = call_sites(ws, s) if not _is_test(fd.path) else Reach()
            via = f" via {'/'.join(reach.via)}" if reach.via else ""
            out.append(
                f"- {s.signature}  [{fd.path}:{s.start}-{s.end}]  call sites: {reach.count()}{via}"
            )
            for path, n, code in reach.sites:
                out.append(f"    {path}:{n}  {code}")
            line = public_api(ws, s, reach)
            if line:
                out.append(f"  ⚠ {line}")
                api_lines.append(line)
    others = [p for p in ws.diffs if p not in {f.path for f in files}]
    if others:
        out.append(
            "Other files changed in this PR (read_diff to see them): "
            + ", ".join(
                f"{p} (+{ws.diffs[p].additions}/-{ws.diffs[p].deletions})"
                for p in others[:30]
            )
        )
    blocks = ["<context>\n" + "\n".join(out) + "\n</context>"] if out else []
    fns = changed_functions(ws, files)
    if fns:
        blocks.append(
            "<changed_functions>\nEach changed function in full at the PR head; `+` marks changed lines. "
            "Use it to check that a fix is complete across the whole function.\n"
            + fns
            + "\n</changed_functions>"
        )
    pats = referenced_patterns(ws, files, pr_text)
    if pats:
        blocks.append(
            "<referenced_code>\nRepo code the change uses or the PR description names — compare the change "
            "against it when the PR claims to follow it.\n"
            + pats
            + "\n</referenced_code>"
        )
    return "\n\n".join(blocks), api_lines
