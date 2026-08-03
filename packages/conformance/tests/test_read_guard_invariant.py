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
the site set from the AST on every run, so a new decode-blind reader fails CI the
moment it lands, wherever it is.

A read is decode-safe when either:

* it goes through ``_ast_common.safe_read_text`` / ``safe_read_json``, or
* its nearest enclosing ``try`` has a handler catching ``UnicodeDecodeError``,
  ``ValueError``, ``Exception``/``BaseException``, or is bare.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CHECKS_ROOT = Path(__file__).resolve().parents[1] / "conformance" / "suite" / "checks"

#: Handler names that do catch a decode error (UnicodeDecodeError is a ValueError).
_DECODE_SAFE = frozenset(
    {"UnicodeDecodeError", "ValueError", "Exception", "BaseException"}
)


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


def _is_path_read_text(node: ast.AST) -> bool:
    """A ``Path.read_text`` call, not ``importlib.metadata.Distribution.read_text``.

    The latter takes a filename positionally and returns ``None`` when absent —
    a different API with no decode step to guard.
    """
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
    ):
        return False
    return any(kw.arg == "encoding" for kw in node.keywords) or not node.args


def _decode_blind_sites(path: Path) -> list[int]:
    """Line numbers of reads in *path* whose nearest handler can't catch a decode error."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
        if _is_path_read_text(node):
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
        "read_text() reachable without a handler that catches UnicodeDecodeError.\n"
        "Use _ast_common.safe_read_text()/safe_read_json(), or widen the guard to\n"
        "include UnicodeDecodeError. Offending sites:\n  " + "\n  ".join(offenders)
    )


def test_the_invariant_check_actually_detects_a_violation(tmp_path: Path) -> None:
    """Guard the guard: a deliberately blind read must be reported.

    Without this, a bug in the AST walk would make the invariant test vacuously
    green — the same 'silence reads as coverage' failure it exists to prevent.
    """
    bad = tmp_path / "blind.py"
    bad.write_text(
        "from pathlib import Path\n"
        "\n"
        "def read(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text(encoding="utf-8")\n'
        "    except OSError:\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(bad) == [5]

    good = tmp_path / "safe.py"
    good.write_text(
        "from pathlib import Path\n"
        "\n"
        "def read(p: Path) -> str:\n"
        "    try:\n"
        '        return p.read_text(encoding="utf-8")\n'
        "    except (OSError, UnicodeDecodeError):\n"
        '        return ""\n',
        encoding="utf-8",
    )
    assert _decode_blind_sites(good) == []
