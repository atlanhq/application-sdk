"""The reviewer's toolset: few, capped, read-only, in-process.

Modelled on open-code-review's distilled review toolset (search, read, find,
read another file's diff, comment, done) with two changes that matter for
cost:

- `find_symbol` answers "where is X defined, who calls it, which tests cover
  it" from the prebuilt index in one call — the lookup a general agent makes
  in five greps.
- Nothing shells out. Search and reads run over the trusted base checkout
  plus the PR-head text fetched as data, so no tool can execute PR code and
  none has a timeout to hang on.

Every result is bounded (lines, matches, bytes) so a tool reply can never be
the thing that blows the context budget.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diff import FileDiff
from .findings import CATEGORIES, SEVERITIES
from .index import SKIP_DIRS, Index

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


def _secret_path(p: str) -> bool:
    name = p.rsplit("/", 1)[-1]
    if name in SAFE_ENV:
        return False
    return any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)


@dataclass
class Workspace:
    """What the tools can see: the base checkout, overlaid with PR-head text."""

    root: Path
    head_text: dict[str, str]  # changed path -> PR-head content ("" = deleted)
    diffs: dict[str, FileDiff]  # every changed file, including ones not under review
    index: Index
    _files: list[str] | None = field(default=None, repr=False)

    def _safe(self, rel: str) -> str | None:
        rel = rel.strip().removeprefix("./")
        if (
            not rel
            or rel.startswith("/")
            or ".." in Path(rel).parts
            or _secret_path(rel)
        ):
            return None
        return rel

    def text(self, rel: str) -> str | None:
        rel = self._safe(rel) or ""
        if not rel:
            return None
        if rel in self.head_text:
            return self.head_text[rel] or None
        p = self.root / rel
        try:
            if (
                p.is_symlink()
                or not p.is_file()
                or not p.resolve().is_relative_to(self.root.resolve())
            ):
                return None
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

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


def read_file(
    ws: Workspace, file_path: str, start_line: int = 1, end_line: int | None = None
) -> str:
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
    needle = search_text if case_sensitive else search_text.lower()
    hits: list[str] = []
    total = 0
    for rel in ws.files():
        if path_glob and not fnmatch.fnmatch(rel, path_glob):
            continue
        text = ws.text(rel)
        if not text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            hay = line if case_sensitive else line.lower()
            if needle in hay:
                total += 1
                if len(hits) < SEARCH_MAX_MATCHES:
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
    if not hits:
        return f"No matches for {search_text!r}."
    more = (
        f"\n… {total - len(hits)} more matches not shown; narrow with path_glob."
        if total > len(hits)
        else ""
    )
    return "\n".join(hits) + more


def find_symbol(ws: Workspace, name: str) -> str:
    name = name.strip().split(".")[-1].split(":")[-1]
    defs = ws.index.definitions(name, limit=4)
    if not defs:
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


def read_diff(ws: Workspace, path_array: list[str]) -> str:
    out = []
    for p in path_array[:5]:
        fd = ws.diffs.get(p)
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
    "required": ["path", "existing_code", "severity", "category", "title", "content"],
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
        f"Fixed-string search across the repo (max {SEARCH_MAX_MATCHES} matches). Use path_glob to narrow, e.g. 'application_sdk/**'.",
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
        {"state": {"type": "string", "enum": ["DONE", "FAILED"]}},
        ["state"],
    ),
]


def parse_args(raw: str) -> dict[str, Any]:
    """Tool arguments as a dict. A malformed payload is salvaged by taking the
    first balanced JSON object in it (open-code-review does the same);
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
