"""No unit test asserts on how much wall-clock time passed.

An assertion like ``assert elapsed < 1.0`` or ``assert lag < 0.1`` measures the
runner, not the code: a loaded 4-vCPU CI runner (GIL contention, a slow
thread-pool spin-up, a 15.6 ms Windows timer tick) misses the bar with nothing
wrong, and the suite goes red on one matrix leg at random (FND-4233).

Each property such a bar stood in for has a deterministic form:

* "the loop stayed free" — :class:`tests.unit.loop_probe.LoopProbe`;
* "a budget was fitted to what is left" — an injected or frozen clock;
* "it returned promptly" — wait on an event, or ``asyncio.wait_for``, bounded
  by a generous timeout that only decides how long a regression takes to fail;
* "it did not wait for the blocked call" — a flag the blocked call sets only
  when it returns, checked after the caller has;
* "it is linear, not quadratic" — count the steps, or size the input so the
  regression takes minutes and bound it far below that.

This scans every test module for an ``assert`` that orders (``<``, ``<=``,
``>``, ``>=``) a clock reading delta, a variable assigned from one, or a value
named like one (``elapsed``, ``lag``, ``ticks``, ``gap``/``gaps``, ``took``).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

_UNIT_ROOT = Path(__file__).parent

#: Attribute calls that read a clock: ``time.monotonic()``, ``loop.time()``.
_CLOCK_ATTRS = frozenset(
    {"monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "time"}
)

#: Bare calls that read a clock, from ``from time import monotonic``.
_CLOCK_NAMES = frozenset({"monotonic", "monotonic_ns", "perf_counter"})

#: Names that hold a timing however they were computed.
_TIMING_NAMES = frozenset({"elapsed", "lag", "ticks", "gap", "gaps", "took"})

#: Files whose remaining sites were kept deliberately, with the issue that kept
#: them. The test fails if an entry finds nothing, so the list only ever shrinks.
_KNOWN = {
    # Cancel-unwind upper bounds, widened to ``_PROMPT = 3.0`` against a 10s
    # self-release rather than made structural.
    "clients/test_sql_client_cancellation.py": "FND-4234",
    # Lower bounds (``>= 1``) that the budget was waited out; an upper bound is
    # what measures load, so these were kept.
    "common/test_restart_marker.py": "FND-4234",
}

_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


@dataclass(frozen=True)
class _Finding:
    path: str
    line: int
    source: str

    def __str__(self) -> str:
        return f"tests/unit/{self.path}:{self.line}: {self.source}"


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _CLOCK_ATTRS
    return isinstance(func, ast.Name) and func.id in _CLOCK_NAMES


def _is_clock_delta(node: ast.AST) -> bool:
    """``node`` contains a subtraction with a clock reading on one side."""
    return any(
        isinstance(sub, ast.BinOp)
        and isinstance(sub.op, ast.Sub)
        and any(
            _is_clock_call(n) for side in (sub.left, sub.right) for n in ast.walk(side)
        )
        for sub in ast.walk(node)
    )


def _names_in(node: ast.AST) -> set[str]:
    """Bare names read in ``node``, including subscripted ones (``lag[0]``).

    An attribute's own name is not a ``Name`` node, so ``attempt.elapsed`` (a
    fake clock's field) does not count.
    """
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _delta_variables(tree: ast.AST) -> set[str]:
    """Names assigned anywhere in the module from a clock delta."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_clock_delta(node.value):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign | ast.AugAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and _is_clock_delta(node.value)
        ):
            found.add(node.target.id)
    return found


def _scan(path: Path, root: Path = _UNIT_ROOT) -> list[_Finding]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    suspects = _TIMING_NAMES | _delta_variables(tree)
    rel = path.relative_to(root).as_posix()
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for compare in ast.walk(node.test):
            if not isinstance(compare, ast.Compare):
                continue
            if not any(isinstance(op, _ORDERING) for op in compare.ops):
                continue
            if _is_clock_delta(compare) or _names_in(compare) & suspects:
                findings.append(
                    _Finding(
                        rel, node.lineno, ast.get_source_segment(source, node) or ""
                    )
                )
                break
    return findings


def _test_modules() -> list[Path]:
    this = Path(__file__).resolve()
    return sorted(
        p
        for p in _UNIT_ROOT.rglob("*.py")
        if p.resolve() != this
        and (p.name.startswith("test_") or p.name == "conftest.py")
    )


def test_no_unit_test_asserts_on_elapsed_wall_clock_time() -> None:
    findings = [
        f for path in _test_modules() for f in _scan(path) if f.path not in _KNOWN
    ]
    assert not findings, (
        "These assertions order a wall-clock timing, so they fail on a loaded "
        "runner with nothing wrong. Assert the property instead (see this "
        "module's docstring):\n" + "\n".join(str(f) for f in findings)
    )


@pytest.mark.parametrize("path", sorted(_KNOWN))
def test_every_known_exemption_still_has_a_site(path: str) -> None:
    """A stale entry would let a new site in that file through unseen."""
    assert _scan(_UNIT_ROOT / path), (
        f"tests/unit/{path} has no wall-clock assertion left; remove it from "
        f"_KNOWN (kept by {_KNOWN[path]})"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "started = time.monotonic()\nwork()\nassert time.monotonic() - started < 1.0",
        "started = time.perf_counter()\nwork()\nelapsed = time.perf_counter() - started\nassert elapsed < 10",
        "t0 = loop.time()\nawait work()\nwaited = loop.time() - t0\nassert waited < 2.0",
        "assert lag[0] < _MAX_LOOP_LAG",
        "assert max(gaps) < 3.0",
        "assert ticks >= expected / 2",
        "start = monotonic()\nwork()\nassert monotonic() - start <= 0.5",
    ],
)
def test_the_scan_catches_each_shape(tmp_path: Path, snippet: str) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(snippet + "\n", encoding="utf-8")
    assert _scan(probe, tmp_path), f"missed: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        # A deadline on a poll loop bounds a wait; it orders a reading, not a delta.
        "deadline = time.monotonic() + 5\nassert time.monotonic() < deadline",
        # A fake clock's field.
        "assert attempts[-1].elapsed >= 0.05",
        # An exact value on a frozen clock.
        "assert monotonic() == 5.0",
        # A bracket: the reading lies between two others, no slack.
        "assert started + 30.0 <= seen <= ended + 30.0",
    ],
)
def test_the_scan_leaves_deterministic_shapes_alone(
    tmp_path: Path, snippet: str
) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(snippet + "\n", encoding="utf-8")
    assert not _scan(probe, tmp_path), f"flagged: {snippet!r}"
