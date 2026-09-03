"""Cross-platform portability rule definitions (P046).

``Path.read_text()``, ``Path.write_text()`` and a text-mode ``open()`` called
without ``encoding=`` decode and encode using the **locale's** preferred
encoding.  That is UTF-8 on the Linux containers the SDK ships on and on macOS,
and **cp1252 on Windows** — which the SDK's own unit matrix runs
(``windows-latest``, 3.11 → 3.14).  The same file therefore reads back
differently depending on where the code runs.

The trigger is narrower than "contains non-ASCII", which is why the shape
survives review: cp1252 maps ``é`` and ``—`` perfectly well and has no mapping
for ``→`` or ``✓``.  So a fixture written with the wrong sample byte passes
against the very bug it means to pin, and a round-trip assertion passes on every
UTF-8 platform.  This rule therefore grades the ``encoding=`` **argument**, not a
round trip.

Scope
-----
``P046`` is ``sdk``-scoped.  The enforcement exists because the SDK repo is the
one that runs a Windows CI leg and ships the library that consumer apps import;
app repos build Linux containers only.  Widening to ``both`` is a one-line change
if a Windows leg ever lands in the fleet, and would be the right call then.

The rule covers every spelling of the hazard, not just the one that broke a
build: ``read_text`` / ``write_text``, the builtin ``open`` and its
signature-compatible aliases, ``Path.open`` and its lookalikes, the text mode of
``gzip`` / ``bz2`` / ``lzma``, and the ``tempfile`` factories.  Banning one
spelling would leave the next one open, which is the failure mode a rule exists
to end.

Rule-id stability (non-migration policy)
----------------------------------------
Rule ids are a permanent public contract — each is exposed in SARIF ``help_uri``
and referenced by inline ``# conformance: ignore[Pxxx]`` suppressions across the
fleet.  An id therefore **never migrates and never changes**.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P046",
        fix_locus=FixLocus.SDK,
        scope=RuleScope.SDK,
        name="LocaleDependentTextIO",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="portability",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.24.0",
        rationale=(
            "Path.read_text()/write_text() and a text-mode open() with no "
            "encoding= use the locale's preferred encoding, so the same file "
            "round-trips differently per platform: UTF-8 on the Linux containers "
            "we ship on and on macOS, cp1252 on Windows, which the SDK's unit "
            "matrix runs. A source file or an evidence artefact containing a "
            "character cp1252 cannot map (an arrow or a tick, while an accented "
            "letter or an em dash passes fine) raises UnicodeDecodeError on "
            "Windows only, so the defect is invisible to exactly the CI legs that "
            "stay green."
        ),
        short_description=(
            "Text file IO without encoding= — read_text/write_text or a "
            "text-mode open() decoding by platform locale"
        ),
        full_description=(
            "``Path.read_text()``, ``Path.write_text()`` and a text-mode\n"
            "``open()`` fall back to ``locale.getpreferredencoding(False)`` when\n"
            "no ``encoding=`` is given.  That resolves to UTF-8 on Linux and\n"
            "macOS and to cp1252 on Windows, so a file written or read without an\n"
            "explicit encoding is only as portable as the characters that happen\n"
            "to be in it.\n"
            "\n"
            "Fix it one of two ways, and the right one depends on what the text\n"
            "is for:\n"
            "\n"
            "* The value is parsed as JSON — ``orjson.loads(path.read_text())`` or\n"
            "  ``json.loads(path.read_text())``.  Both parsers accept ``bytes``\n"
            "  natively, so use ``path.read_bytes()``: the decode step is a wasted\n"
            "  round trip *and* is the entire source of the locale dependency.\n"
            '  Adding ``encoding="utf-8"`` here papers over a conversion that\n'
            "  should not be happening.\n"
            '* Everything else — pass ``encoding="utf-8"`` explicitly, or read\n'
            "  through the suite's own ``safe_read_text(path)``\n"
            "  (``conformance.suite.checks._ast_common``), which defaults to UTF-8\n"
            "  and returns ``None`` rather than raising on undecodable bytes.\n"
            "\n"
            "Only fully implicit calls are flagged.  ``read_text(encoding=...)``,\n"
            '``read_text("utf-8")`` (the encoding is ``read_text``\'s first\n'
            "positional parameter), ``write_text(data, encoding=...)`` and\n"
            '``write_text(data, "utf-8")`` all pass, as does any call carrying a\n'
            "``**kwargs`` splat the checker cannot see into.  The decode-free\n"
            "``importlib.metadata.Distribution.read_text(filename)`` lookalike\n"
            "passes because it takes a positional argument.\n"
            "\n"
            "For ``open``-like calls, only **text mode** is graded — binary reads\n"
            "and writes decode nothing — and the families do not agree on how to\n"
            "spell it:\n"
            "\n"
            "* the builtin ``open`` / ``io.open`` / ``aiofiles.open`` and\n"
            '``<path>.open(...)`` are text unless the mode says ``"b"``.\n'
            "\n"
            "* ``gzip`` / ``bz2`` / ``lzma`` are binary by default **and** binary\n"
            'for any mode without a ``"t"``, so ``gzip.open(p, "w")`` is not graded\n'
            'while ``gzip.open(p, "wt")`` is.\n'
            "\n"
            "* the ``tempfile`` factories are binary by default (``w+b``) but read\n"
            'an explicit mode the builtin\'s way — text whenever ``"b"`` is absent\n'
            '— so ``NamedTemporaryFile(mode="w")`` is graded.\n'
            "\n"
            "Receivers whose ``open`` never yields a decoded stream are skipped\n"
            "outright (``os`` returns a file descriptor, ``tarfile`` / ``zipfile``\n"
            "return archive members, ``codecs.open`` is binary without an\n"
            "encoding), as is ``SafeFileOps.open`` — the SDK wrapper resolves\n"
            "UTF-8 for its callers, so a call site passing no encoding is already\n"
            "correct.\n"
            "\n"
            "Suppress a reviewed exception with a justification:\n"
            "``# conformance: ignore[P046] <reason>``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
            "conformance/docs/rules/prescriptions.md#p046"
        ),
    ),
)
