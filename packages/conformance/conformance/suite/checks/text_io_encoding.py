"""P046 LocaleDependentTextIO — text file IO with no explicit ``encoding=``.

``Path.read_text()``, ``Path.write_text()`` and a text-mode ``open()`` with no
``encoding=`` fall back to ``locale.getpreferredencoding(False)``: UTF-8 on the
Linux containers the SDK ships on and on macOS, **cp1252 on Windows**, which the
SDK's unit matrix runs.  A file therefore round-trips differently depending on
where the code runs, and the trigger is narrower than "contains non-ASCII" —
cp1252 maps ``é`` and ``—`` fine and has no mapping for ``→`` or ``✓``.  A
round-trip assertion is blind to this on every UTF-8 platform, so the check
grades the ``encoding=`` **argument** instead.

The rule covers the whole class rather than one spelling of it.  Banning
``read_text`` alone would leave ``open(path)`` — the same defect, one keyword
away — as an open door, and the point of a rule over a sweep is that it stops
the next site, not the last one.

Matching — ``read_text`` / ``write_text``
-----------------------------------------
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

Matching — ``open`` and friends
-------------------------------
An ``open``-like call only decodes in **text mode**, and each family spells both
"text mode" and "where encoding sits" differently, so the callee is classified
into a per-family signature (see :class:`_OpenSignature`) rather than matched
by one shape:

* the builtin ``open`` / ``io.open`` / ``aiofiles.open`` — text by default;
* ``<expr>.open(...)`` (``Path.open`` and every lookalike) — text by default,
  but with ``mode`` one position earlier because there is no path argument;
* ``gzip`` / ``bz2`` / ``lzma`` ``.open`` — **binary** by default, and text only
  on a ``"t"`` in the mode, so the same rule catches ``gzip.open(p, "wt")``
  while leaving ``gzip.open(p, "w")`` (binary) alone;
* the ``tempfile`` factories — binary by default, but reading an explicit mode
  the builtin's way, so ``NamedTemporaryFile(mode="w")`` is text.
  ``SpooledTemporaryFile`` takes a ``max_size`` first and so carries its own
  signature with both indices shifted by one.

Receivers whose ``open`` never yields a decoded text stream are skipped outright
(``os`` returns a descriptor; ``tarfile``/``zipfile`` return archive members;
``codecs.open`` is binary without an encoding).  ``SafeFileOps.open`` is skipped
for the opposite reason: it resolves UTF-8 for its callers, so flagging them
would report the wrapper's whole purpose as a defect.

The one asymmetry is deliberate.  A mode the checker cannot read counts as text
for the builtin — the name is unambiguous, so a dynamic-mode call with no
encoding is a real risk — and is **skipped** for ``<expr>.open``, where matching
is receiver-blind and that shape is dominated by lookalikes passing something
else first (``zipfile_handle.open(member)``).

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
from dataclasses import dataclass
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

_MESSAGE_OPEN = (
    "Text-mode open() with no encoding= uses the platform locale (UTF-8 on "
    "Linux/macOS, cp1252 on Windows — which the SDK's unit matrix runs), so the "
    'same file reads back differently per platform. Pass encoding="utf-8", or '
    "open in binary mode if the payload is bytes. Suppress a reviewed exception "
    "with '# conformance: ignore[P046] <reason>'."
)


@dataclass(frozen=True)
class _OpenSignature:
    """Where ``mode`` and ``encoding`` sit in one ``open``-like signature.

    An ``open``-like call is a locale hazard only in *text* mode, and every
    family spells that differently, so the family — not one hard-coded shape —
    is what decides.  The builtin and ``Path.open`` differ by one position
    because the former takes the path first.  The compression and ``tempfile``
    families both default to *binary*, but they disagree on how an explicit mode
    reads — ``gzip.open(p, "w")`` is binary while
    ``NamedTemporaryFile(mode="w")`` is text — which is why the default and the
    spelling are two fields rather than one.
    """

    mode_index: int
    """Positional index of ``mode``."""
    encoding_index: int
    """Positional index of ``encoding``."""
    default_is_text: bool
    """Whether omitting ``mode`` yields a text stream."""
    explicit_text_needs_t: bool
    """How to read a mode string: ``True`` means text requires a ``"t"``
    (the compressed-stream families, where a bare ``"w"`` is binary);
    ``False`` means text is anything without a ``"b"`` (every other family,
    including ``tempfile`` — whose default is binary, yet whose ``mode="w"``
    is text).  Kept separate from :attr:`default_is_text` because the two do
    not move together: ``tempfile`` defaults to binary and reads its modes the
    builtin's way."""
    unknown_mode_is_text: bool | None
    """Verdict for a mode this checker cannot read (``None`` = skip the call)."""


#: ``open(file, mode="r", buffering, encoding, ...)`` — the builtin and its
#: signature-compatible aliases.  A mode the checker cannot read still counts as
#: text: the name ``open`` is unambiguous here, so there is no lookalike to
#: mistake it for, and a dynamic-mode call with no encoding is a real risk.
_BUILTIN_OPEN = _OpenSignature(
    mode_index=1,
    encoding_index=3,
    default_is_text=True,
    explicit_text_needs_t=False,
    unknown_mode_is_text=True,
)

#: ``Path.open(mode="r", buffering, encoding, ...)`` — and everything else that
#: merely *spells* a method ``open``.  Matching is receiver-blind, so an
#: unreadable mode here is more likely a lookalike's first argument (a zipfile
#: member name, a wrapper's path) than a genuine dynamic mode — hence skip.
_PATH_OPEN = _OpenSignature(
    mode_index=0,
    encoding_index=2,
    default_is_text=True,
    explicit_text_needs_t=False,
    unknown_mode_is_text=None,
)

#: ``gzip.open(filename, mode="rb", compresslevel, encoding, ...)`` and the
#: bz2/lzma twins: binary by default, text only when the mode carries a ``"t"``.
_COMPRESSED_OPEN = _OpenSignature(
    mode_index=1,
    encoding_index=3,
    default_is_text=False,
    explicit_text_needs_t=True,
    unknown_mode_is_text=None,
)

#: ``tempfile.NamedTemporaryFile(mode="w+b", buffering, encoding, ...)`` and
#: ``TemporaryFile``: binary by default, but reading an explicit mode the
#: builtin's way, so ``mode="w"`` is text.
_TEMPFILE_FACTORY = _OpenSignature(
    mode_index=0,
    encoding_index=2,
    default_is_text=False,
    explicit_text_needs_t=False,
    unknown_mode_is_text=None,
)

#: ``tempfile.SpooledTemporaryFile(max_size=0, mode="w+b", buffering, encoding,
#: ...)`` — the one sibling that takes a *size* first, so both indices shift by
#: one.  Sharing :data:`_TEMPFILE_FACTORY` would read ``max_size`` as the mode
#: (silently skipping ``SpooledTemporaryFile(1024, "w")``, whose first
#: positional is an int) and would not recognise a positional encoding.
_SPOOLED_TEMPFILE_FACTORY = _OpenSignature(
    mode_index=1,
    encoding_index=3,
    default_is_text=False,
    explicit_text_needs_t=False,
    unknown_mode_is_text=None,
)

#: Module receivers whose ``open`` takes the builtin's signature.
_BUILTIN_OPEN_RECEIVERS: frozenset[str] = frozenset({"io", "aiofiles"})

#: Module receivers whose ``open`` is the binary-default compressed-stream form.
_COMPRESSED_OPEN_RECEIVERS: frozenset[str] = frozenset({"gzip", "bz2", "lzma"})

#: ``tempfile`` factories, keyed to the signature each one actually has.
_TEMPFILE_FACTORIES: dict[str, _OpenSignature] = {
    "NamedTemporaryFile": _TEMPFILE_FACTORY,
    "TemporaryFile": _TEMPFILE_FACTORY,
    "SpooledTemporaryFile": _SPOOLED_TEMPFILE_FACTORY,
}

#: Receivers whose ``open`` never produces a locale-decoded text stream: it
#: returns a file *descriptor* (``os``), an archive or archive member
#: (``tarfile``, ``zipfile``), a database/URL handle (``shelve``, ``dbm``,
#: ``sqlite3``, ``webbrowser``, ``socket``), or a stream that is binary unless
#: an encoding is passed (``codecs``).
_NON_TEXT_OPEN_RECEIVERS: frozenset[str] = frozenset(
    {
        "os",
        "tarfile",
        "zipfile",
        "shelve",
        "dbm",
        "sqlite3",
        "socket",
        "webbrowser",
        "wave",
        "codecs",
    }
)

#: SDK wrappers that resolve UTF-8 for their callers, so a call site passing no
#: ``encoding`` is correct by construction.  ``SafeFileOps.open`` mirrors the
#: builtin signature and defaults its own ``encoding`` to UTF-8 in text mode
#: (``application_sdk.common.file_ops``); flagging its callers would report the
#: wrapper's whole reason for existing as a defect.
_ENCODING_RESOLVING_WRAPPERS: frozenset[str] = frozenset({"SafeFileOps"})


def _lacks_encoding(node: ast.Call, *, positional_index: int) -> bool:
    """True if *node* passes no encoding, positionally or by keyword.

    *positional_index* is where ``encoding`` sits in the callee's signature: 0
    for ``read_text(encoding, errors, newline)``, 1 for
    ``write_text(data, encoding, errors, newline)``.  A ``**kwargs`` splat is
    opaque, so it counts as "encoding possibly supplied" and clears the call.
    """
    if any(kw.arg is None for kw in node.keywords):
        return False
    if any(kw.arg == "encoding" for kw in node.keywords):
        return False
    return len(node.args) <= positional_index


def _receiver_name(value: ast.expr) -> str | None:
    """Return the receiver's leading name (``os`` for ``os.path``), else ``None``."""
    while isinstance(value, ast.Attribute):
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def _open_signature(func: ast.expr) -> _OpenSignature | None:
    """Classify an ``open``-like callee, or ``None`` if it is not one.

    Bare ``open(...)``/``NamedTemporaryFile(...)`` names are matched directly —
    ``from tempfile import NamedTemporaryFile`` is as common as the dotted form.
    """
    if isinstance(func, ast.Name):
        if func.id == "open":
            return _BUILTIN_OPEN
        return _TEMPFILE_FACTORIES.get(func.id)
    if not isinstance(func, ast.Attribute):
        return None

    receiver = _receiver_name(func.value)
    if func.attr in _TEMPFILE_FACTORIES:
        return _TEMPFILE_FACTORIES[func.attr]
    if func.attr != "open":
        return None
    if receiver in _NON_TEXT_OPEN_RECEIVERS or receiver in _ENCODING_RESOLVING_WRAPPERS:
        return None
    if receiver in _BUILTIN_OPEN_RECEIVERS:
        return _BUILTIN_OPEN
    if receiver in _COMPRESSED_OPEN_RECEIVERS:
        return _COMPRESSED_OPEN
    return _PATH_OPEN


def _mode_argument(node: ast.Call, signature: _OpenSignature) -> ast.expr | None:
    """Return the ``mode`` argument expression, positional or keyword."""
    if len(node.args) > signature.mode_index:
        return node.args[signature.mode_index]
    for kw in node.keywords:
        if kw.arg == "mode":
            return kw.value
    return None


def _opens_in_text_mode(node: ast.Call, signature: _OpenSignature) -> bool:
    """True if *node* is provably (or conservatively) a text-mode open."""
    mode = _mode_argument(node, signature)
    if mode is None:
        return signature.default_is_text
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        if signature.explicit_text_needs_t:
            return "t" in mode.value
        return "b" not in mode.value
    return bool(signature.unknown_mode_is_text)


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

        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "read_text",
            "write_text",
        }:
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
        else:
            signature = _open_signature(node.func)
            if (
                signature is not None
                and _opens_in_text_mode(node, signature)
                and _lacks_encoding(node, positional_index=signature.encoding_index)
            ):
                self._add(node, _MESSAGE_OPEN)

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
