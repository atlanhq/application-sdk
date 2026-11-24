"""No code under ``application_sdk/`` measures elapsed time with ``time.time()``.

``time.time()`` is the wall clock: NTP, a VM resume or an operator can step it
by any amount, in either direction, between two readings. A duration taken as
the difference of two of them comes out negative or hours long when that
happens, and it lands in ACTIVITY_END events, request logs and traces as if it
were real. ``time.monotonic()`` never steps, which is what a duration needs.

The wall clock stays right for a *timestamp* (when something happened) and for
comparing against a time that came from somewhere else (a token's ``exp``, a
file's mtime, ``server_now`` from a clock-offset calibration). This scan flags
the two shapes that are an elapsed measurement:

* ``time.time() - <name>``: a fresh reading minus a name or attribute (the
  earlier reading, however it got there: a local, a parameter, ``self.<attr>``);
* ``<a> - <b>`` where both names were assigned a ``time.time()`` reading in the
  same module (followed through plain assignment and ``self.<attr> =``), as in
  ``end - start``.

A reading held in a name minus a value from elsewhere (``now - last_push``,
where ``last_push`` was parsed from a Pushgateway response) is an age against
another clock and is left alone, as is a right side that is a call or a
subscript (``os.path.getmtime(p)``, ``record["ts"]``).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

_SDK_ROOT = Path(__file__).resolve().parents[2] / "application_sdk"


@dataclass(frozen=True)
class _Aliases:
    """How a module spells the wall clock.

    ``modules``: names bound to the ``time`` module (``import time``,
    ``import time as clock``). ``functions``: names bound to ``time.time``
    itself (``from time import time``, ``from time import time as wall``).
    """

    modules: frozenset[str]
    functions: frozenset[str]

    @classmethod
    def of(cls, nodes: list[ast.AST]) -> _Aliases:
        modules: set[str] = set()
        functions: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                modules.update(
                    a.asname or a.name for a in node.names if a.name == "time"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "time":
                functions.update(
                    a.asname or a.name for a in node.names if a.name == "time"
                )
        return cls(frozenset(modules), frozenset(functions))


def _is_wall_clock_call(node: ast.AST, aliases: _Aliases) -> bool:
    """A no-argument call of ``time.time``, however this module imported it."""
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "time"
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases.modules
        )
    return isinstance(func, ast.Name) and func.id in aliases.functions


def _target_key(node: ast.AST) -> str | None:
    """``name`` for a bare name, ``self.attr`` for an attribute on a name."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _wall_clock_names(nodes: list[ast.AST], aliases: _Aliases) -> set[str]:
    """Names (and ``obj.attr`` targets) assigned straight from a wall-clock read."""
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _is_wall_clock_call(value, aliases) or _target_key(value) in found:
            found.update(k for t in targets if (k := _target_key(t)) is not None)
    return found


@dataclass(frozen=True)
class _Finding:
    path: str
    line: int
    source: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.source}"


def _scan(path: Path, root: Path) -> list[_Finding]:
    source = path.read_text(encoding="utf-8")
    if "time" not in source:
        return []
    tree = ast.parse(source, filename=str(path))
    nodes = list(ast.walk(tree))
    aliases = _Aliases.of(nodes)
    names = _wall_clock_names(nodes, aliases)

    def is_elapsed(node: ast.BinOp) -> bool:
        right = _target_key(node.right)
        if right is None:
            return False
        if _is_wall_clock_call(node.left, aliases):
            return True
        return _target_key(node.left) in names and right in names

    rel = path.relative_to(root).as_posix()
    return [
        _Finding(rel, node.lineno, ast.get_source_segment(source, node) or "")
        for node in nodes
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and is_elapsed(node)
    ]


def test_no_elapsed_time_measured_with_the_wall_clock() -> None:
    findings = [
        f
        for path in sorted(_SDK_ROOT.rglob("*.py"))
        for f in _scan(path, _SDK_ROOT.parent)
    ]
    assert not findings, (
        "These subtract two time.time() readings to get a duration; a clock step "
        "between them makes it negative or huge. Use time.monotonic() for both "
        "readings:\n" + "\n".join(str(f) for f in findings)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "import time\nstart = time.time()\nwork()\nd = time.time() - start",
        "import time\nstart = time.time()\nd = (time.time() - start) * 1000",
        "import time\nstart = time.time()\nend = time.time()\nd = end - start",
        "from time import time\nstart = time()\nd = time() - start",
        "import time\nclass C:\n    def a(self):\n        self.t0 = time.time()\n"
        "    def b(self):\n        return time.time() - self.t0",
        "from time import time\nclass C:\n    def a(self):\n        self._last = time()\n"
        "    def b(self):\n        now = time()\n        return now - self._last",
        "import time\nt: float = time.time()\nd = time.time() - t",
        "import time\nbegin = time.time()\nstart = begin\nd = time.time() - start",
        # Aliased imports.
        "import time as clock\nstart = clock.time()\nd = clock.time() - start",
        "from time import time as wall\nstart = wall()\nd = wall() - start",
        # The start reading arrives as a parameter (observability_decorator).
        "import time\ndef f(start_time):\n    return (time.time() - start_time) * 1000",
    ],
)
def test_the_scan_catches_each_shape(tmp_path: Path, snippet: str) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(snippet + "\n", encoding="utf-8")
    assert _scan(probe, tmp_path), f"missed: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        # The monotonic clock is the fix.
        "import time\nstart = time.monotonic()\nd = time.monotonic() - start",
        # Time left until a deadline that came from elsewhere.
        "import time\nleft = exp_ts - time.time()",
        # Age of something whose timestamp was not read from this clock here.
        "import time\nage = time.time() - os.path.getmtime(p)",
        "import time\nage = time.time() - record['timestamp']",
        # A timestamp, not a duration.
        "import time\nnow_ms = int(time.time() * 1000)",
        # A held reading minus a time parsed from elsewhere (pushgateway sweep).
        "import time\nnow = time.time()\nlast_push = float(ts)\nage = now - last_push",
        # A different module's ``time`` attribute (``workflow.time()``).
        "start = workflow.time()\nd = workflow.time() - start",
        # ``x.time()`` where ``x`` is not the ``time`` module.
        "import time as clock\nstart = time.time()\nd = time.time() - start",
        # ``time()`` without ``from time import time`` is some other function.
        "start = time()\nd = time() - start",
    ],
)
def test_the_scan_leaves_other_shapes_alone(tmp_path: Path, snippet: str) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(snippet + "\n", encoding="utf-8")
    assert not _scan(probe, tmp_path), f"flagged: {snippet!r}"
