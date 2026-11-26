"""Fail when a Markdown code example uses a deprecated SDK symbol (FND-4268).

The SDK's docs taught `SqlMetadataExtractor`, `HandlerError`,
`PreflightStatus.PARTIAL` and other deprecated symbols as the v3 path, so apps
that followed them landed on a deprecated base and had to migrate again.

Every Python code block in a tracked Markdown file is checked with the
conformance package's own B001 matcher (`scan_consumer`) against the committed
`deprecated_symbols.json`. Docs and apps are therefore judged by one reader:
an import resolves through re-exports (`from application_sdk.templates import
SqlMetadataExtractor`) exactly as it does in an app, and an enum member
(`PreflightStatus.PARTIAL`) is matched through the enum it was imported with.
Method and property records are left out: those match by bare attribute name,
which in prose examples hits unrelated receivers.

A block that teaches a deprecated symbol on purpose (a v2 "before" snippet, a
section about a deprecated framework) opts out with a marker on the line
directly above its opening fence, giving the reason:

    <!-- legacy-example: v2 "before" snippet -->
    ```python
    from application_sdk.handler import HandlerError
    ```

Doc examples are often fragments (`...` bodies, `await` at top level, bare
statements). A block that does not parse is retried with each offending line
blanked, so the lines that do parse are still checked.

Usage: check_docs_deprecated_imports.py [FILE.md ...]
With no arguments, checks every Markdown file `git ls-files` lists.
Exits 1 and prints `path:line: message` per finding.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from conformance.suite.checks.deprecation import scan_consumer
from conformance.suite.checks.deprecation._manifest import Manifest, load_manifest

#: Generated or historical files: they record what was, not what to write.
EXCLUDED = frozenset({"CHANGELOG.md", "packages/conformance/CHANGELOG.md"})

#: Record kinds matched by where they were imported from. Methods and
#: properties match by attribute name alone, so they are left out.
_MODULE_AWARE_KINDS = frozenset({"class", "function", "constant", "enum_member"})

_PYTHON_TAGS = frozenset({"", "python", "py", "python3", "pycon"})

_FENCE_OPEN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*\{?\.?(?P<tag>[\w+-]*)"
)
_MARKER = re.compile(r"^\s*<!--\s*legacy-example:\s*(?P<reason>\S.*?)\s*-->\s*$")
_BARE_MARKER = re.compile(r"^\s*<!--\s*legacy-example\b")


@dataclass(frozen=True)
class CodeBlock:
    """One fenced code block: its body and the file line of its first body line."""

    first_line: int
    body: str
    marked: bool


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _dedent(line: str, indent: str) -> str:
    return line[len(indent) :] if line.startswith(indent) else line.lstrip()


def iter_python_blocks(text: str) -> Iterator[CodeBlock]:
    """Yield every fenced block tagged as Python (or untagged) in *text*."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opened = _FENCE_OPEN.match(lines[index])
        if opened is None:
            index += 1
            continue
        indent, fence, tag = opened["indent"], opened["fence"], opened["tag"]
        end = index + 1
        while end < len(lines) and not lines[end].strip().startswith(fence):
            end += 1
        if tag.lower() in _PYTHON_TAGS:
            above = lines[index - 1] if index > 0 else ""
            yield CodeBlock(
                first_line=index + 2,
                body="\n".join(
                    _dedent(line, indent) for line in lines[index + 1 : end]
                ),
                marked=_MARKER.match(above) is not None,
            )
        index = end + 1


def bare_markers(text: str) -> list[int]:
    """Line numbers of `legacy-example` markers that give no reason."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if _BARE_MARKER.match(line) and not _MARKER.match(line)
    ]


def parse_leniently(body: str) -> ast.Module | None:
    """Parse *body*, blanking each line a SyntaxError points at, then retrying.

    Line numbers are kept, so findings still point at the right file line.
    Returns None only when nothing parseable is left.
    """
    lines = body.splitlines()
    for _ in range(len(lines) + 1):
        try:
            return ast.parse("\n".join(lines))
        except SyntaxError as exc:
            lineno = exc.lineno
            if (
                lineno is None
                or not 1 <= lineno <= len(lines)
                or not lines[lineno - 1].strip()
            ):
                lineno = _last_nonblank(lines)
            if lineno is None:
                return None
            lines[lineno - 1] = ""
    return None


def _last_nonblank(lines: list[str]) -> int | None:
    for number in range(len(lines), 0, -1):
        if lines[number - 1].strip():
            return number
    return None


def module_aware(manifest: Manifest) -> Manifest:
    return Manifest(
        symbols=tuple(s for s in manifest.symbols if s.kind in _MODULE_AWARE_KINDS)
    )


def find_violations(path: str, text: str, manifest: Manifest) -> list[Violation]:
    """Return a violation per deprecated-symbol use in an unmarked block of *text*."""
    violations = [
        Violation(
            path,
            line,
            "legacy-example marker needs a reason: <!-- legacy-example: why -->",
        )
        for line in bare_markers(text)
    ]
    for block in iter_python_blocks(text):
        if block.marked or "application_sdk" not in block.body:
            continue
        tree = parse_leniently(block.body)
        if tree is None:
            continue
        for finding in scan_consumer(tree, path, manifest, {}):
            violations.append(
                Violation(path, block.first_line + finding.line - 1, finding.message)
            )
    return sorted(set(violations), key=lambda v: (v.path, v.line, v.message))


def tracked_markdown() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in listed.split("\0") if p and p not in EXCLUDED]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paths = [a for a in args if a not in EXCLUDED] if args else tracked_markdown()
    manifest = module_aware(load_manifest())
    if not manifest.symbols:
        print("deprecated_symbols.json is missing or empty; nothing to check against")
        return 1
    violations = [
        violation
        for path in paths
        for violation in find_violations(
            path, Path(path).read_text(encoding="utf-8"), manifest
        )
    ]
    for violation in violations:
        print(violation)
    if violations:
        print(
            f"\n{len(violations)} use(s) of deprecated SDK symbols in doc examples. "
            "Rewrite the example onto the replacement the message names, or, if "
            "the block shows the deprecated API on purpose, put "
            "`<!-- legacy-example: <reason> -->` on the line above its fence. "
            "See docs/standards/documentation.md."
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
