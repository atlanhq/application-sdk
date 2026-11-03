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
import functools
import re
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

_CANCEL_UNWIND = 'assert elapsed < _PROMPT, f"loop blocked {elapsed:.2f}s on cancel"'

#: Assertions kept deliberately, by file and exact source, with how many times
#: each occurs and the issue that kept it. Matched exactly: one more copy, or a
#: new assertion in the same file, is a finding; one fewer fails as stale, so
#: the list only ever shrinks.
_KNOWN: dict[tuple[str, str], tuple[int, str]] = {
    # Cancel-unwind upper bounds, widened to ``_PROMPT = 3.0`` against a 10s
    # self-release rather than made structural.
    ("clients/test_sql_client_cancellation.py", _CANCEL_UNWIND): (5, "FND-4234"),
    (
        "clients/test_sql_client_cancellation.py",
        'assert ticks > before, "loop did not run other tasks after the cancel"',
    ): (1, "FND-4234"),
    # Lower bounds that the budget was waited out; an upper bound is what
    # measures load, so these were kept.
    (
        "common/test_restart_marker.py",
        'assert loop.time() - started >= 1, "returned before the budget was spent"',
    ): (1, "FND-4234"),
    (
        "common/test_restart_marker.py",
        'assert loop.time() - started >= 1, "a scheduled eviction must be waited for"',
    ): (1, "FND-4234"),
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


@dataclass(frozen=True)
class _Reads:
    """What an expression reads: the bare names in it, and whether it calls a clock.

    Bare names include subscripted ones (``lag[0]``). An attribute's own name is
    not a ``Name`` node, so ``attempt.elapsed`` (a fake clock's field) does not
    count.
    """

    names: frozenset[str]
    calls_clock: bool

    @classmethod
    def of(cls, node: ast.AST) -> _Reads:
        names: set[str] = set()
        calls_clock = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif not calls_clock:
                calls_clock = _is_clock_call(sub)
        return cls(frozenset(names), calls_clock)

    def reads_clock(self, clock_vars: set[str]) -> bool:
        return self.calls_clock or not self.names.isdisjoint(clock_vars)


@dataclass(frozen=True)
class _Expr:
    """An expression summarised once, so the scan walks each node a bounded number of times.

    ``subtractions`` holds both sides of every ``a - b`` inside it.
    """

    reads: _Reads
    subtractions: tuple[tuple[_Reads, _Reads], ...]

    @classmethod
    def of(cls, node: ast.AST) -> _Expr:
        names: set[str] = set()
        calls_clock = False
        subtractions: list[tuple[_Reads, _Reads]] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Sub):
                subtractions.append((_Reads.of(sub.left), _Reads.of(sub.right)))
            elif not calls_clock:
                calls_clock = _is_clock_call(sub)
        return cls(_Reads(frozenset(names), calls_clock), tuple(subtractions))

    def is_clock_delta(self, clock_vars: set[str]) -> bool:
        """A subtraction with a clock reading on one side.

        The reading may be inline (``time.monotonic() - started``) or held in a
        variable (``end - start`` after ``start = time.monotonic()``).
        """
        return any(
            left.reads_clock(clock_vars) or right.reads_clock(clock_vars)
            for left, right in self.subtractions
        )


def _assignments(nodes: list[ast.AST]) -> list[tuple[str, _Expr]]:
    """Every ``name = value`` in the module, as (name, value)."""
    pairs: list[tuple[str, _Expr]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if targets:
                value = _Expr.of(node.value)
                pairs.extend((name, value) for name in targets)
        elif (
            isinstance(node, ast.AnnAssign | ast.AugAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            pairs.append((node.target.id, _Expr.of(node.value)))
    return pairs


def _clock_variables(pairs: list[tuple[str, _Expr]]) -> set[str]:
    """Names holding a clock reading, followed through plain reassignment."""
    found: set[str] = set()
    while True:
        more = {
            name
            for name, value in pairs
            if name not in found and value.reads.reads_clock(found)
        }
        if not more:
            return found
        found |= more


#: A module can only hold a finding if its text names a clock or a timing.
_MENTIONS_TIMING = re.compile(
    r"monotonic|perf_counter|\.time\(\)|\b(?:"
    + "|".join(sorted(_TIMING_NAMES))
    + r")\b"
)


def _scan(path: Path, root: Path = _UNIT_ROOT) -> list[_Finding]:
    source = path.read_text(encoding="utf-8")
    if not _MENTIONS_TIMING.search(source):
        return []
    nodes = list(ast.walk(ast.parse(source, filename=str(path))))
    asserts = [n for n in nodes if isinstance(n, ast.Assert)]
    if not asserts:
        return []
    pairs = _assignments(nodes)
    clock_vars = _clock_variables(pairs)
    suspects = _TIMING_NAMES | {
        name for name, value in pairs if value.is_clock_delta(clock_vars)
    }
    rel = path.relative_to(root).as_posix()
    findings = []
    for node in asserts:
        for compare in ast.walk(node.test):
            if not isinstance(compare, ast.Compare):
                continue
            if not any(isinstance(op, _ORDERING) for op in compare.ops):
                continue
            expr = _Expr.of(compare)
            if expr.is_clock_delta(clock_vars) or not expr.reads.names.isdisjoint(
                suspects
            ):
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


@functools.cache
def _findings_by_site() -> dict[tuple[str, str], list[_Finding]]:
    """Every finding in the suite, by (file, source); scanned once per worker."""
    sites: dict[tuple[str, str], list[_Finding]] = {}
    for path in _test_modules():
        for f in _scan(path):
            sites.setdefault((f.path, f.source), []).append(f)
    return sites


def test_no_unit_test_asserts_on_elapsed_wall_clock_time() -> None:
    findings = [
        f
        for site, found in _findings_by_site().items()
        if len(found) > _KNOWN.get(site, (0, ""))[0]
        for f in found
    ]
    assert not findings, (
        "These assertions order a wall-clock timing, so they fail on a loaded "
        "runner with nothing wrong. Assert the property instead (see this "
        "module's docstring):\n" + "\n".join(str(f) for f in findings)
    )


@pytest.mark.parametrize("site", sorted(_KNOWN), ids=lambda site: site[0])
def test_every_known_exemption_still_matches_exactly(site: tuple[str, str]) -> None:
    """A stale count would leave room for a new copy of the site to slip in."""
    count, issue = _KNOWN[site]
    found = len(_findings_by_site().get(site, []))
    assert found == count, (
        f"tests/unit/{site[0]} now has {found} of {site[1]!r}, not {count}; "
        f"update _KNOWN (kept by {issue})"
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
        "start = time.monotonic()\nend = time.monotonic()\nassert end - start < 1",
        "t0 = time.perf_counter()\nt1 = time.perf_counter()\nduration = t1 - t0\nassert duration < 1",
        "begin = time.monotonic()\nstart = begin\nassert time.monotonic() - start < 1",
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
