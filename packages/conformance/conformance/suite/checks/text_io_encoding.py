"""P046 LocaleDependentTextIO — encoding-less ``read_text`` / ``write_text``.

``Path.read_text()`` and ``Path.write_text()`` with no ``encoding=`` fall back to
``locale.getpreferredencoding(False)``: UTF-8 on the Linux containers the SDK
ships on and on macOS, **cp1252 on Windows**, which the SDK's unit matrix runs.
A file therefore round-trips differently depending on where the code runs, and
the trigger is narrower than "contains non-ASCII" — cp1252 maps ``é`` and ``—``
fine and has no mapping for ``→`` or ``✓``.  A round-trip assertion is blind to
this on every UTF-8 platform, so the check grades the ``encoding=`` **argument**
instead.

Matching
--------
Attribute-name anchored, like the suite's own read-guard invariant: any
``.read_text(...)`` / ``.write_text(...)`` call, without receiver-type inference.
A call is flagged only when the encoding argument is **fully absent**:

* ``read_text`` — encoding is the first positional parameter, so any positional
  argument or an ``encoding=`` keyword clears it.  That is also what keeps the
  decode-free ``importlib.metadata.Distribution.read_text(filename)`` lookalike
  quiet, with no per-site exemption marker needed.
* ``write_text`` — encoding is the *second* positional parameter (after the
  payload), so two positionals or an ``encoding=`` keyword clears it.
* A ``**kwargs`` splat clears either: the checker cannot see whether it carries
  ``encoding``, and a false positive on an unreadable call is worse than a miss.

Matching on the argument rather than on a round trip is deliberate — a round-trip
fixture passes against this bug on every UTF-8 platform, which is every leg that
stays green when the Windows legs go red.

The remedy in the message is site-specific.  When the read feeds
``orjson.loads`` / ``json.loads`` — both accept ``bytes`` natively — the fix is
``read_bytes()``, not an added ``encoding=`` kwarg: the decode is a wasted round
trip *and* the entire source of the locale dependency.  Everywhere else the
remedy names ``encoding="utf-8"`` and the shared ``safe_read_text`` helper.

Scope
-----
``P046`` is ``sdk``-scoped (see ``suite.rules.portability``).  Discovery is the
shared Python-source walk — which already drops ``tests/`` — **extended** with
the sibling packages under ``packages/``.  The shared walk excludes any path
component named ``conformance``, which would otherwise hide the conformance
package's own sources from a rule that governs them; ``packages/`` exists only in
the SDK repo, so the extension is inert everywhere else.

Inline suppression
------------------
``# conformance: ignore[P046] <reason>`` on the offending line, or on the
comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    EXCLUDE_DIRS,
    _IgnoreDirective,
    _parse_directives,
)
from conformance.suite.checks._ast_common import discover as _shared_discover
from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_finding,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_ID = "P046"

__all__ = ["RULE_ID", "SERIES", "discover", "main", "scan_path", "scan_text"]

#: Parser functions that accept ``bytes`` directly, so a ``read_text`` feeding one
#: should become ``read_bytes()`` rather than growing an ``encoding=`` kwarg.
_BYTES_PARSERS: frozenset[str] = frozenset({"loads"})

#: Sibling-package root the shared walk cannot reach — see the module docstring.
_PACKAGES_DIR = "packages"

#: Exclusions for the ``packages/`` walk: the standard policy minus the
#: ``conformance`` component, which is exactly the subtree this rule must see.
_PACKAGES_EXCLUDE_DIRS: frozenset[str] = EXCLUDE_DIRS - {"conformance"}

_MESSAGE_READ = (
    "Path.read_text() with no encoding= decodes using the platform locale "
    "(UTF-8 on Linux/macOS, cp1252 on Windows — which the SDK's unit matrix "
    'runs). Pass encoding="utf-8", or read through '
    "conformance.suite.checks._ast_common.safe_read_text(), which defaults to "
    "UTF-8 and returns None on undecodable bytes. Suppress a reviewed exception "
    "with '# conformance: ignore[P046] <reason>'."
)

_MESSAGE_READ_FOR_PARSER = (
    "Path.read_text() with no encoding= decodes using the platform locale "
    "(UTF-8 on Linux/macOS, cp1252 on Windows — which the SDK's unit matrix "
    "runs). This read feeds {parser}(), which accepts bytes natively — use "
    "path.read_bytes() rather than adding encoding=: the decode is a wasted "
    "round trip and is the entire source of the locale dependency. Suppress a "
    "reviewed exception with '# conformance: ignore[P046] <reason>'."
)

_MESSAGE_WRITE = (
    "Path.write_text() with no encoding= encodes using the platform locale "
    "(UTF-8 on Linux/macOS, cp1252 on Windows — which the SDK's unit matrix "
    "runs), so a character cp1252 cannot map raises on Windows only. Pass "
    'encoding="utf-8". Suppress a reviewed exception with '
    "'# conformance: ignore[P046] <reason>'."
)


def _lacks_encoding(node: ast.Call, *, positional_index: int) -> bool:
    """True if *node* passes no encoding, positionally or by keyword.

    *positional_index* is where ``encoding`` sits in the method's signature: 0
    for ``read_text(encoding, errors, newline)``, 1 for
    ``write_text(data, encoding, errors, newline)``.  A ``**kwargs`` splat is
    opaque, so it counts as "encoding possibly supplied" and clears the call.
    """
    if any(kw.arg is None for kw in node.keywords):
        return False
    if any(kw.arg == "encoding" for kw in node.keywords):
        return False
    return len(node.args) <= positional_index


def _is_read_text_call(node: ast.AST) -> bool:
    """True if *node* is a ``<expr>.read_text(...)`` call, in any argument shape."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
    )


def _parser_name(func: ast.expr) -> str | None:
    """Return the display name of a bytes-accepting parser call, else ``None``.

    Matches ``orjson.loads(...)`` / ``json.loads(...)`` by attribute and a bare
    imported ``loads(...)`` by name.  The receiver is included when there is one
    so the remedy can name the parser it actually saw.
    """
    if isinstance(func, ast.Attribute) and func.attr in _BYTES_PARSERS:
        receiver = func.value
        if isinstance(receiver, ast.Name):
            return f"{receiver.id}.{func.attr}"
        return func.attr
    if isinstance(func, ast.Name) and func.id in _BYTES_PARSERS:
        return func.id
    return None


class _TextIoChecker(ast.NodeVisitor):
    """Walk a module AST and emit P046 findings."""

    def __init__(self, filename: str, directives: dict[int, _IgnoreDirective]) -> None:
        self._filename = filename
        self._directives = directives
        self.findings: list[Finding] = []
        #: ``id(node)`` → parser display name, populated when the enclosing
        #: parser call is visited (top-down) and read back when the nested
        #: ``read_text`` call is reached.
        self._reads_feeding_parser: dict[int, str] = {}

    def visit_Call(self, node: ast.Call) -> None:
        parser = _parser_name(node.func)
        if parser is not None:
            for arg in node.args:
                if _is_read_text_call(arg):
                    self._reads_feeding_parser[id(arg)] = parser

        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "read_text" and _lacks_encoding(
                node, positional_index=0
            ):
                parser_for_site = self._reads_feeding_parser.get(id(node))
                self._add(
                    node,
                    _MESSAGE_READ_FOR_PARSER.format(parser=parser_for_site)
                    if parser_for_site
                    else _MESSAGE_READ,
                )
            elif node.func.attr == "write_text" and _lacks_encoding(
                node, positional_index=1
            ):
                self._add(node, _MESSAGE_WRITE)

        self.generic_visit(node)

    def _add(self, node: ast.AST, message: str) -> None:
        self.findings.append(
            make_finding(
                filename=self._filename,
                rule_id=RULE_ID,
                node=node,
                message=message,
                directives=self._directives,
            )
        )


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for P046 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    checker = _TextIoChecker(filename=file, directives=_parse_directives(text))
    checker.visit(tree)
    return checker.findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file for P046 findings."""
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def discover(root: Path) -> list[Path]:
    """Shared Python-source discovery, extended to the ``packages/`` siblings.

    The shared walk drops any path component named ``conformance``, so the
    conformance package's own sources are invisible to it.  This rule governs
    them — they ship as Python and run on the same Windows leg — so the
    ``packages/`` subtree is walked once more with that one exclusion lifted.
    Everything else stays excluded, ``tests/`` included, and ``packages/`` only
    exists in the SDK repo, so this is inert on a consumer app.
    """
    found = dict.fromkeys(_shared_discover(root))
    packages_dir = root / _PACKAGES_DIR
    if packages_dir.is_dir():
        for path in _shared_discover(packages_dir, exclude_dirs=_PACKAGES_EXCLUDE_DIRS):
            found.setdefault(path, None)
    return sorted(found)


main = make_cli_main(
    scan_text,
    description="P046: flag encoding-less Path.read_text()/write_text() calls.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())
