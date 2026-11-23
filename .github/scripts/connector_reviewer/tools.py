"""The reviewer's toolset: few, capped, read-only, in-process.

A small review toolset (search, read, find, read another file's diff,
comment, done), with two properties that matter for cost:

- `find_symbol` answers "where is X defined, who calls it, which tests cover
  it" from the prebuilt index in one call — the lookup a general agent makes
  in five greps.
- Nothing shells out. Search and reads run over the trusted base checkout
  plus the PR-head text fetched as data, so no tool can execute PR code and
  none has a timeout to hang on.

Every result is bounded (lines, matches, bytes) so a tool reply can never be
the thing that blows the context budget.

When the app's application-sdk pin resolved (sdk_source.py), read_file,
search_code and find_symbol also reach that SDK source, read-only, for a path
or name that starts with `sdk:` (`SdkSource`). The same path guard applies, and
only `application_sdk/` is reachable.
"""

from __future__ import annotations

import fnmatch
import html
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sdk_source
from .diff import FileDiff
from .findings import CATEGORIES, SEVERITIES
from .index import SKIP_DIRS, Index, index_source

READ_MAX_LINES = 250
SEARCH_MAX_MATCHES = 40
SEARCH_EXTS = {
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".md",
    ".pkl",
    ".cfg",
    ".ini",
    ".json",
    ".sh",
    ".txt",
}
SECRET_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.jks",
    "*id_rsa*",
    "*id_ed25519*",
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "*/.npmrc",
    "*/.pypirc",
    "*/.netrc",
    "*.keytab",
)
SAFE_ENV = (".env.example", ".env.sample", ".env.template")


def secret_diff(fd: FileDiff) -> bool:
    """True when either side of this change may hold secrets. A rename from
    `.env` to an ordinary name still carries the deleted `.env` lines in its
    diff, so the source path counts as much as the destination."""
    return secret_path(fd.path) or (
        fd.old_path is not None and secret_path(fd.old_path)
    )


def secret_path(p: str) -> bool:
    name = p.rsplit("/", 1)[-1]
    if name in SAFE_ENV:
        return False
    return any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)


def _safe_rel(rel: str) -> str | None:
    """`rel` as a plain relative path, or None when it is empty, absolute,
    climbs out with `..`, or may hold secrets."""
    rel = rel.strip().removeprefix("./")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts or secret_path(rel):
        return None
    return rel


def _read_inside(
    root: Path, rel: str, inside: Path, max_bytes: int | None = None
) -> str | None:
    """The text of `root/rel`, only when it is a regular file, not a symlink,
    and resolves inside `inside` (so a symlinked directory on the way cannot
    lead out of it either)."""
    p = root / rel
    try:
        if (
            p.is_symlink()
            or not p.is_file()
            or not p.resolve().is_relative_to(inside.resolve())
            or (max_bytes is not None and p.stat().st_size > max_bytes)
        ):
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ---- the application-sdk source (FND-5331) -------------------------------------

# The prefix that sends read_file / search_code / find_symbol to the SDK source
# instead of the app: `sdk:application_sdk/outputs/__init__.py`.
SDK_PREFIX = "sdk:"
# Bounds that keep the SDK (about 460 Python files, 8 MB at v3.58) from slowing a
# review: the file list, one file's size, and the index's build time. A file or
# index past them is left out, and the tool says so.
SDK_MAX_FILES = 5_000
SDK_MAX_FILE_BYTES = 1_000_000
SDK_INDEX_SECONDS = 20.0


@dataclass
class SdkSource:
    """The application-sdk checkout at the version the app pins: read-only, the
    `application_sdk/` package only. Nothing here is under review and nothing
    here is overlaid with pull-request text.

    The file list and the index are built on first use, once, under a lock:
    bundles are reviewed in parallel over one Workspace, and a review that
    never asks about the SDK never walks it."""

    root: Path
    label: str  # "v3.40.0", or "3.40.0 at commit abc123def456"
    _files: list[str] | None = field(default=None, repr=False)
    _index: Index | None = field(default=None, repr=False)
    index_partial: bool = False
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @classmethod
    def open(cls, checkout: str, app_root: Path) -> tuple[SdkSource | None, str]:
        """The SDK source at `checkout`, labelled with the pin in the app's
        uv.lock (the same `sdk_source.resolve_root` that chose the checkout's
        ref), or None and the reason it is not available."""
        res = sdk_source.resolve_root(app_root)
        if res.pin is None:
            return None, f"SDK source not available: {res.reason}"
        path = Path(checkout) if checkout.strip() else None
        if path is None or not sdk_source.checkout_present(path):
            return None, (
                f"SDK source not available: no checkout of application-sdk at "
                f"{res.pin.label} (the ref may not exist)"
            )
        return cls(root=path.resolve(), label=res.pin.label), ""

    def _allowed(self, rel: str) -> str | None:
        rel = _safe_rel(rel) or ""
        return rel if rel.startswith(sdk_source.PACKAGE_DIR + "/") else None

    def text(self, rel: str) -> str | None:
        rel = self._allowed(rel) or ""
        if not rel:
            return None
        return _read_inside(
            self.root,
            rel,
            self.root / sdk_source.PACKAGE_DIR,
            max_bytes=SDK_MAX_FILE_BYTES,
        )

    def files(self) -> list[str]:
        with self._lock:
            if self._files is None:
                self._files = self._walk()
            return self._files

    def _walk(self) -> list[str]:
        out: list[str] = []
        pkg = self.root / sdk_source.PACKAGE_DIR
        if pkg.is_symlink():
            return out
        # os.walk does not follow symlinked directories; symlinked files are
        # skipped here and refused again by `text`.
        for dirpath, dirnames, filenames in os.walk(pkg):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            )
            for f in sorted(filenames):
                p = Path(dirpath) / f
                if p.suffix not in SEARCH_EXTS or p.is_symlink():
                    continue
                rel = str(p.relative_to(self.root))
                if self._allowed(rel):
                    out.append(rel)
                if len(out) >= SDK_MAX_FILES:
                    return out
        return out

    def index(self) -> Index:
        """The SDK's symbol index, built on the first `find_symbol("sdk:…")`
        and stopped at `SDK_INDEX_SECONDS` (what was indexed by then is kept,
        and `index_partial` says so)."""
        files = self.files()
        with self._lock:
            if self._index is None:
                idx = Index(root=str(self.root))
                deadline = time.monotonic() + SDK_INDEX_SECONDS
                for rel in files:
                    if not rel.endswith(".py"):
                        continue
                    if time.monotonic() > deadline:
                        self.index_partial = True
                        break
                    text = self.text(rel)
                    if text:
                        try:
                            index_source(idx, rel, text)
                        except (SyntaxError, ValueError, RecursionError):
                            continue
                idx._link()
                self._index = idx
            return self._index

    def prompt_block(self) -> str:
        """What the line review is told about the SDK source, in its context
        message. The system prompt does not change, so a review without the SDK
        source sends the same prompt as before."""
        label = html.escape(self.label, quote=True)
        return (
            f'<sdk_source version="{label}">\n'
            f"The Atlan Application SDK source this app pins in uv.lock ({label}) is "
            "readable, read-only, through the same tools with an `sdk:` prefix:\n"
            '- read_file(file_path="sdk:application_sdk/<path>.py")\n'
            '- search_code(search_text=..., path_glob="sdk:application_sdk/**")\n'
            '- find_symbol(name="sdk:<Name>")\n'
            "Use it when the diff calls an SDK API (outputs, publish, typed errors, storage, "
            "workflows and tasks) and a finding depends on how that API behaves: its signature "
            "and defaults, what it raises, retries or writes, and what it expects of the caller. "
            "Check that in the source instead of assuming it. The SDK is reference, not under "
            "review: comment only on files in <review_files>.\n"
            "</sdk_source>"
        )


@dataclass
class Workspace:
    """What the tools can see: the base checkout, overlaid with PR-head text,
    and, when the app's pin resolved, the SDK source under `sdk:`. `text` and
    `files` are the app only: the context and facts code walks them, and the
    SDK is reached through the tools alone."""

    root: Path
    head_text: dict[str, str]  # changed path -> PR-head content ("" = deleted)
    diffs: dict[str, FileDiff]  # every changed file, including ones not under review
    index: Index
    sdk: SdkSource | None = None
    _files: list[str] | None = field(default=None, repr=False)

    def _safe(self, rel: str) -> str | None:
        return _safe_rel(rel)

    def text(self, rel: str) -> str | None:
        rel = self._safe(rel) or ""
        if not rel:
            return None
        if rel in self.head_text:
            return self.head_text[rel] or None
        return _read_inside(self.root, rel, self.root)

    def files(self) -> list[str]:
        if self._files is None:
            out: list[str] = []
            for p in self.root.rglob("*"):
                rel = p.relative_to(self.root)
                if any(
                    part in SKIP_DIRS
                    or part.startswith(".")
                    and part not in (".github",)
                    for part in rel.parts[:-1]
                ):
                    continue
                if p.suffix in SEARCH_EXTS and p.is_file() and not p.is_symlink():
                    out.append(str(rel))
            out.extend(k for k, v in self.head_text.items() if v and k not in out)
            self._files = sorted(
                set(out) - {k for k, v in self.head_text.items() if not v}
            )
        return self._files


# ---- tool implementations ----------------------------------------------------


SDK_UNAVAILABLE = (
    "ERROR: the application-sdk source is not available in this review; `sdk:` paths "
    "and names cannot be read."
)


def _sdk_part(value: str | None) -> str | None:
    """What follows `sdk:` in a tool argument, or None when it has no prefix."""
    v = (value or "").strip()
    return v[len(SDK_PREFIX) :] if v.startswith(SDK_PREFIX) else None


def read_file(
    ws: Workspace, file_path: str, start_line: int = 1, end_line: int | None = None
) -> str:
    sdk_rel = _sdk_part(file_path)
    if sdk_rel is not None:
        if ws.sdk is None:
            return SDK_UNAVAILABLE
        text = ws.sdk.text(sdk_rel)
        if text is None:
            return (
                f"ERROR: cannot read {file_path!r} (missing, outside application_sdk/, "
                "too large, or a secret-bearing path)."
            )
    else:
        text = ws.text(file_path)
    if text is None:
        return f"ERROR: cannot read {file_path!r} (missing, outside the repo, or a secret-bearing path)."
    lines = text.splitlines()
    start = max(int(start_line or 1), 1)
    end = min(
        int(end_line or start + READ_MAX_LINES - 1),
        len(lines),
        start + READ_MAX_LINES - 1,
    )
    body = "\n".join(f"{i:>5} {lines[i - 1]}" for i in range(start, end + 1))
    trunc = end < len(lines) and (end_line is None or end < int(end_line))
    return f"File: {file_path} (total {len(lines)} lines, showing {start}-{end}{', TRUNCATED' if trunc else ''})\n{body}"


def search_code(
    ws: Workspace,
    search_text: str,
    path_glob: str | None = None,
    case_sensitive: bool = True,
) -> str:
    if not search_text or len(search_text) < 3:
        return "ERROR: search_text must be at least 3 characters."
    # A path_glob starting `sdk:` searches the SDK source instead of the app,
    # and every hit carries the prefix so it can be handed back to read_file.
    sdk_glob = _sdk_part(path_glob)
    if sdk_glob is not None:
        if ws.sdk is None:
            return SDK_UNAVAILABLE
        files, read, shown = ws.sdk.files(), ws.sdk.text, SDK_PREFIX
        path_glob = sdk_glob or None
    else:
        files, read, shown = ws.files(), ws.text, ""
    needle = search_text if case_sensitive else search_text.lower()
    hits: list[str] = []
    total = 0
    for rel in files:
        if path_glob and not fnmatch.fnmatch(rel, path_glob):
            continue
        text = read(rel)
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            hay = line if case_sensitive else line.lower()
            if needle in hay:
                total += 1
                if len(hits) < SEARCH_MAX_MATCHES:
                    hits.append(f"{shown}{rel}:{i}: {line.strip()[:200]}")
    if not hits:
        return f"No matches for {search_text!r}."
    more = (
        f"\n… {total - len(hits)} more matches not shown; narrow with path_glob."
        if total > len(hits)
        else ""
    )
    return "\n".join(hits) + more


def find_symbol(ws: Workspace, name: str) -> str:
    # Before the split below, which would strip the `sdk:` prefix away.
    sdk_name = _sdk_part(name)
    if sdk_name is not None:
        return SDK_UNAVAILABLE if ws.sdk is None else sdk_symbol(ws.sdk, sdk_name)
    name = name.strip().split(".")[-1].split(":")[-1]
    defs = ws.index.definitions(name, limit=4)
    if not defs:
        if ws.sdk is not None:
            return (
                f"No definition of {name!r} in the index. If it comes from the SDK, "
                f'find_symbol(name="sdk:{name}") looks in the SDK source.'
            )
        return f"No definition of {name!r} in the index."
    out: list[str] = []
    for d in defs:
        out.append(
            f"DEFINED {d.path}:{d.start}-{d.end}  {d.signature}"
            + (f"  — {d.doc}" if d.doc else "")
        )
        tests = ws.index.tests_for.get(d.path, [])[:4]
        out.append(
            "  tests importing this module: " + (", ".join(tests) if tests else "NONE")
        )
    callers = ws.index.callers_of(name, limit=8)
    out.append(
        f"CALLERS ({len(ws.index.callers.get(name, []))} total, showing {len(callers)}):"
    )
    for c in callers:
        out.append(f"  {c.path}:{c.start}  {c.qualname.split(':')[-1]}  {c.signature}")
    return "\n".join(out)


def sdk_symbol(sdk: SdkSource, name: str) -> str:
    """`find_symbol` over the SDK source: definitions and the SDK's own callers.
    Its tests are not checked out, so none are listed."""
    name = name.strip().split(".")[-1].split(":")[-1]
    idx = sdk.index()
    partial = (
        " (the SDK index is partial: it stopped at its time limit)"
        if sdk.index_partial
        else ""
    )
    defs = idx.definitions(name, limit=4) if name else []
    if not defs:
        return f"No definition of {name!r} in the SDK source ({sdk.label}){partial}."
    out = [f"SDK source {sdk.label}{partial}:"]
    for d in defs:
        out.append(
            f"DEFINED {SDK_PREFIX}{d.path}:{d.start}-{d.end}  {d.signature}"
            + (f"  — {d.doc}" if d.doc else "")
        )
    callers = idx.callers_of(name, limit=8)
    out.append(
        f"CALLERS inside the SDK ({len(idx.callers.get(name, []))} total, showing {len(callers)}):"
    )
    for c in callers:
        out.append(
            f"  {SDK_PREFIX}{c.path}:{c.start}  {c.qualname.split(':')[-1]}  {c.signature}"
        )
    return "\n".join(out)


def read_diff(ws: Workspace, path_array: list[str]) -> str:
    out: list[str] = []
    for p in path_array[:5]:
        fd = ws.diffs.get(p)
        # `ws.diffs` holds every changed file, including ones selection
        # skipped, so the guard is applied here too, to both sides of a rename.
        if secret_path(p) or (fd is not None and secret_diff(fd)):
            out.append(f'<file path="{p}">\nnot available: may hold secrets\n</file>')
            continue
        out.append(
            f'<file path="{p}">\n{fd.render(max_lines=200) if fd else "not changed in this PR"}\n</file>'
        )
    return "\n".join(out)


# ---- schemas -----------------------------------------------------------------

COMMENT_ITEM = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "A file from <review_files>."},
        "existing_code": {
            "type": "string",
            "description": "The exact code this comment is about, copied VERBATIM from the diff (1-6 lines, no line numbers). The position is computed from this.",
        },
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "compatibility": {
            "type": "boolean",
            "description": "true when the harm lands on something that existed BEFORE this PR: existing callers, a deployed contract, stored or persisted data, workflow replay, wire values. false for a defect in what the new code itself does.",
        },
        "principle": {
            "type": "string",
            "description": "The id of the <principle> this finding rests on (FP01, RP02, …), one that applies to this file. Omit only for a concrete bug or security defect; any other finding without a principle is dropped.",
        },
        "title": {"type": "string", "description": "One line, under 90 characters."},
        "content": {
            "type": "string",
            "description": "What is wrong and the concrete input/state under which it fails. 1-4 sentences.",
        },
        "suggestion_code": {
            "type": "string",
            "description": "Optional. Replacement for existing_code, complete and drop-in (same indentation). Omit if the fix is not a local edit.",
        },
    },
    "required": [
        "path",
        "existing_code",
        "severity",
        "category",
        "compatibility",
        "title",
        "content",
    ],
}


def _fn(
    name: str, desc: str, props: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


TOOL_SCHEMAS = [
    _fn(
        "find_symbol",
        "Where a function/class/method is defined, its signature, who calls it and which tests import its module. Cheapest way to check a contract.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _fn(
        "read_file",
        f"Read lines of a file at the PR head (max {READ_MAX_LINES} lines per call).",
        {
            "file_path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        ["file_path"],
    ),
    _fn(
        "search_code",
        f"Fixed-string search across the repo (max {SEARCH_MAX_MATCHES} matches). Use path_glob to narrow, e.g. 'app/**'.",
        {
            "search_text": {"type": "string"},
            "path_glob": {"type": "string"},
            "case_sensitive": {"type": "boolean"},
        },
        ["search_text"],
    ),
    _fn(
        "read_diff",
        "Read the diff of other files changed in this PR (not in your review set).",
        {"path_array": {"type": "array", "items": {"type": "string"}}},
        ["path_array"],
    ),
    _fn(
        "code_comment",
        "Report confirmed defects. Call once with every finding; each must quote existing_code verbatim.",
        {"comments": {"type": "array", "items": COMMENT_ITEM}},
        ["comments"],
    ),
    _fn(
        "task_done",
        "Call when every file in <review_files> has had its pass.",
        {
            "state": {"type": "string", "enum": ["DONE", "FAILED"]},
            "regression": {
                "type": "string",
                "description": "One or two sentences: can this change break behaviour that works today, and would a test in the right tier catch it? Rest it on <regression_facts>.",
            },
            "history": {
                "type": "string",
                "description": "Only when <history_facts> is present, one or two sentences: is this going back and forth, and does the PR say why?",
            },
        },
        ["state"],
    ),
]


def parse_args(raw: str) -> dict[str, Any]:
    """Tool arguments as a dict. A malformed payload is salvaged by taking the
    first balanced JSON object in it;
    anything else is an empty dict and the tool reports the error."""
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        start = (raw or "").find("{")
        depth = 0
        for i in range(max(start, 0), len(raw or "")):
            depth += {"{": 1, "}": -1}.get(raw[i], 0)
            if depth == 0 and start >= 0:
                try:
                    v = json.loads(raw[start : i + 1])
                    return v if isinstance(v, dict) else {}
                except json.JSONDecodeError:
                    return {}
        return {}


def run_tool(ws: Workspace, name: str, args: dict[str, Any]) -> str:
    try:
        if name == "find_symbol":
            return find_symbol(ws, str(args.get("name", "")))
        if name == "read_file":
            return read_file(
                ws,
                str(args.get("file_path", "")),
                args.get("start_line") or 1,
                args.get("end_line"),
            )
        if name == "search_code":
            return search_code(
                ws,
                str(args.get("search_text", "")),
                args.get("path_glob"),
                bool(args.get("case_sensitive", True)),
            )
        if name == "read_diff":
            return read_diff(ws, [str(p) for p in (args.get("path_array") or [])])
    except (TypeError, ValueError) as e:
        return f"ERROR: bad arguments for {name}: {e}"
    return f"ERROR: unknown tool {name!r}."
