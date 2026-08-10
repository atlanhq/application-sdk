"""Suite-wide invariant: no checker read can escape as a decode error.

``Path.read_text(encoding=...)`` raises ``UnicodeDecodeError`` on undecodable
bytes.  That is a ``ValueError``, **not** an ``OSError``, so the reflexive
``except OSError:`` guard does not catch it.  ``runner.py`` wraps neither
``discover()`` nor ``scan_all()``/``scan_path()``, so one stray byte in a
consumer repo aborts the whole multi-series run on whichever series reaches it
first — every later series then never executes and the run reports nothing.
Silence reads as coverage, which is the failure this suite exists to catch.

This class recurred across four review rounds because each round fixed the sites
a review had *enumerated*, and the next round found a reader nobody had listed.
The fix is mechanical enforcement rather than a fifth list: this test re-derives
the site set from the AST on every run, so a new decode-blind reader fails CI
the moment it lands, wherever it is.

A read is decode-safe when either:

* it goes through ``_ast_common.safe_read_text`` / ``safe_read_json``, or
* its nearest enclosing ``try`` has a handler catching ``UnicodeDecodeError``,
  ``ValueError``, ``Exception``/``BaseException``, or is bare.

The gate matches the *property* (any text-mode read of file bytes) rather than
enumerating argument shapes: ``read_text(...)`` in any form, ``Path.open(...)``
and builtin ``open(...)`` in text mode.  Argument *shape* is the wrong
discriminator — ``p.read_text("utf-8")`` (positional encoding) decodes exactly
like the keyword form, and ``open(p, encoding="utf-8")`` is at least as
idiomatic.  The one decode-free lookalike,
``importlib.metadata.Distribution.read_text`` (a filename lookup with no decode
step), is exempted explicitly at its single site with ``# read-guard: exempt``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CHECKS_ROOT = Path(__file__).resolve().parents[1] / "conformance" / "suite" / "checks"

#: Handler names that do catch a decode error (UnicodeDecodeError is a ValueError).
_DECODE_SAFE = frozenset(
    {"UnicodeDecodeError", "ValueError", "Exception", "BaseException"}
)

#: Comment that exempts a decode-free lookalike (Distribution.read_text).
_EXEMPT = "# read-guard: exempt"


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["bare"]
    if isinstance(handler.type, ast.Name):
        return [handler.type.id]
    if isinstance(handler.type, ast.Attribute):
        return [handler.type.attr]
    if isinstance(handler.type, ast.Tuple):
        names: list[str] = []
        for element in handler.type.elts:
            if isinstance(element, ast.Name):
                names.append(element.id)
            elif isinstance(element, ast.Attribute):
                names.append(element.attr)
        return names
    return []


def _is_text_read(node: ast.AST) -> bool:
    """A text-mode read of file bytes, in any argument shape.

    Matches the property, not the spelling: ``Path.read_text(...)`` however
    called, ``<expr>.open(...)`` and builtin ``open(...)`` when text mode is in
    effect (no mode argument, a keyword like ``encoding=``/``errors=``/
    ``newline=``, or a mode string without ``b``).  Binary-mode opens return
    bytes — no decode, nothing to guard.
    """
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
        # Path.read_text decodes no matter the argument shape; the decode-free
        # Distribution.read_text lookalike is exempted at its site.
        return True
    if isinstance(node.func, ast.Attribute):
        if node.func.attr != "open":
            return False
    elif isinstance(node.func, ast.Name):
        if node.func.id != "open":
            return False
    else:
        return False
    return _is_text_mode_open(node)


def _is_text_mode_open(node: ast.Call) -> bool:
    """Whether an ``open`` call yields a text stream (so .read() can decode)."""
    if any(kw.arg in {"encoding", "errors", "newline"} for kw in node.keywords):
        return True
    # Mode is the second positional argument (or `mode=` keyword); default "r".
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return True
    if not isinstance(mode, ast.Constant):
        # A dynamic mode (`open(path, mode)`) cannot be proven binary, so treat
        # it as potentially text — the decode-blind class this gate exists to
        # end must not slip through a non-constant mode expression.
        return True
    return "b" not in str(mode.value)


def _exempt_lines(path: Path) -> set[int]:
    """Lines carrying the explicit ``# read-guard: exempt`` marker."""
    return {
        lineno
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _EXEMPT in line
    }


#: Positional string constants that mark a decode-shaped ``read_text`` call —
#: ``Path.read_text("utf-8")`` passes its encoding positionally, and the
#: exemption must not clear it just because no ``encoding`` *keyword* is
#: present.  The decode-free lookalike takes a *filename* positionally, and no
#: real filename is an encoding name.
_KNOWN_ENCODINGS = frozenset(
    {
        "ascii",
        "latin-1",
        "latin1",
        "utf-8",
        "utf8",
        "utf-16",
        "utf16",
        "utf-32",
        "utf32",
        "cp1252",
        "iso-8859-1",
    }
)


def _is_exemptable_lookalike(node: ast.AST) -> bool:
    """Whether *node* is the decode-free lookalike the exemption exists for.

    ``importlib.metadata.Distribution.read_text(filename)`` takes a filename
    positionally and returns ``None`` when absent — no decode step.  A
    ``Path.read_text`` decodes and must never be exempted, so the marker only
    honours a ``.read_text(<positional>)`` call with no ``encoding`` keyword —
    the lookalike's signature — and refuses a keyword/decode-shaped call.  A
    *positional* known-encoding string (``p.read_text("utf-8")``) is equally
    decode-shaped, so it refuses the exemption too.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
        and not any(kw.arg == "encoding" for kw in node.keywords)
        and not any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.lower() in _KNOWN_ENCODINGS
            for arg in node.args
        )
    )


def _decode_blind_sites(path: Path) -> list[int]:
    """Line numbers of reads in *path* whose nearest handler can't catch a decode error."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exempt = _exempt_lines(path)
    blind: list[int] = []

    def walk(node: ast.AST, enclosing: list[ast.Try]) -> None:
        if isinstance(node, ast.Try):
            for child in node.body:
                walk(child, [*enclosing, node])
            for handler in node.handlers:
                for child in handler.body:
                    walk(child, enclosing)
            for child in [*node.orelse, *node.finalbody]:
                walk(child, enclosing)
            return
        if _is_text_read(node):
            # The exemption only clears the decode-free lookalike, never a real
            # decode-risky read that happens to share the marker's line.
            if node.lineno in exempt and _is_exemptable_lookalike(node):
                return
            names = [
                name
                for try_node in enclosing
                for handler in try_node.handlers
                for name in _handler_names(handler)
            ]
            if not any(n in _DECODE_SAFE or n == "bare" for n in names):
                blind.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, enclosing)

    walk(tree, [])
    return blind


def test_no_decode_blind_reads_under_suite_checks() -> None:
    """Every read under suite/checks/ must survive undecodable bytes."""
    offenders: list[str] = []
    for path in sorted(_CHECKS_ROOT.rglob("*.py")):
        for lineno in _decode_blind_sites(path):
            rel = path.relative_to(_CHECKS_ROOT.parents[2])
            offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "text read reachable without a handler that catches UnicodeDecodeError.\n"
        "Use _ast_common.safe_read_text()/safe_read_json(), or widen the guard to\n"
        "include UnicodeDecodeError. A genuinely decode-free lookalike (e.g.\n"
        "importlib.metadata.Distribution.read_text) needs `# read-guard: exempt`\n"
        "on its line. Offending sites:\n  " + "\n  ".join(offenders)
    )


def test_the_invariant_check_actually_detects_a_violation(tmp_path: Path) -> None:
    """Guard the guard: deliberately blind reads must be reported.

    Without this, a bug in the AST walk would make the invariant test vacuously
    green — the same 'silence reads as coverage' failure it exists to prevent.
    Every matched shape is exercised here so a narrowing regression in any one
    of them fails loudly.
    """
    bad = tmp_path / "blind.py"
    bad.write_text(
        "from pathlib import Path\n"
        "\n"
        "def read(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text(encoding="utf-8")\n'
        "    except OSError:\n"
        '        return ""\n'
        "\n"
        "def read_positional(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text("utf-8")\n'
        "    except OSError:\n"
        '        return ""\n'
        "\n"
        "def read_open(p: Path) -> str:\n"
        "    try:\n"
        '        with open(p, encoding="utf-8") as fh:\n'
        "            return fh.read()\n"
        "    except OSError:\n"
        '        return ""\n'
        "\n"
        "def read_path_open(p: Path) -> str:\n"
        "    try:\n"
        '        with p.open(encoding="utf-8") as fh:\n'
        "            return fh.read()\n"
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(bad) == [5, 11, 17, 24]

    good = tmp_path / "safe.py"
    good.write_text(
        "from pathlib import Path\n"
        "\n"
        "def read(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text(encoding="utf-8")\n'
        "    except (OSError, UnicodeDecodeError):\n"
        '        return ""\n'
        "\n"
        "def read_binary(p: Path) -> bytes:\n"
        "    try:\n"
        '        with open(p, "rb") as fh:\n'
        "            return fh.read()\n"
        "    except OSError:\n"
        '        return b""\n'
        "\n"
        "def read_exempt(dist) -> str:\n"
        "    try:\n"
        '        return dist.read_text("top_level.txt")  # read-guard: exempt\n'
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(good) == []


def test_dynamic_mode_open_is_treated_as_text(tmp_path: Path) -> None:
    """`open(path, mode)` with a non-constant mode is potentially text.

    A mode the walker cannot statically prove binary must not let a decode-blind
    read through — treating it as text is the conservative direction for a gate
    whose whole purpose is that silence reads as coverage.
    """
    src = tmp_path / "dyn.py"
    src.write_text(
        "def read(p, mode) -> str:\n"
        "    try:\n"
        "        with open(p, mode) as fh:\n"
        "            return fh.read()\n"
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(src) == [3]


def test_exemption_refuses_a_decode_risky_read(tmp_path: Path) -> None:
    """`# read-guard: exempt` must not clear a real `Path.read_text`.

    The marker exists only for the decode-free `Distribution.read_text`
    lookalike. A keyword/decode-shaped `read_text` sharing the marker's line is
    a genuine decode risk and must still be reported.
    """
    src = tmp_path / "abuse.py"
    src.write_text(
        "from pathlib import Path\n"
        "\n"
        "def read(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text(encoding="utf-8")  # read-guard: exempt\n'
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(src) == [5]


def test_exemption_refuses_a_positional_encoding(tmp_path: Path) -> None:
    """A positional-encoding `read_text("utf-8")` is decode-shaped too.

    `Path.read_text` takes the encoding as its first positional argument, so a
    call spelled without the `encoding` keyword decodes exactly like the
    keyword form — the exemption matched only on the keyword's absence and
    cleared it. Any positional known-encoding constant now refuses the marker,
    while the lookalike's filename positional (no encoding name is a real
    filename) still clears.
    """
    src = tmp_path / "abuse_positional.py"
    src.write_text(
        "from pathlib import Path\n"
        "\n"
        "def read(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text("utf-8")  # read-guard: exempt\n'
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(src) == [5]

    lookalike = tmp_path / "lookalike.py"
    lookalike.write_text(
        "import importlib.metadata\n"
        "\n"
        "def top_level(dist: importlib.metadata.Distribution) -> str:\n"
        "    try:\n"
        '        return dist.read_text("top_level.txt") or ""  # read-guard: exempt\n'
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(lookalike) == []
