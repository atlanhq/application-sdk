"""No unit test injects a failure and then asserts nothing.

A "must not raise" test sets ``side_effect=SomeError(...)`` on a mock and
calls the code under test. With no assertion after, it cannot tell "the call
failed and was swallowed" from "the call never ran": a flipped gate, an early
return or a renamed patch target leaves it green (FND-5482). Two of the tests
this guard first found had been passing that way: the fault was never reached.

Assert the fault-injected mock was called (``assert_called_once``,
``assert_awaited_once``), and where the contract is "logged and swallowed",
the log line too. A test that injects the failure to prove a call is *not*
made (``side_effect=AssertionError("listed")``) asserts ``assert_not_called()``,
which also fails if a broad ``except`` swallowed the ``AssertionError``.

This scans every unit test function for an exception ``side_effect`` (as a
keyword, an attribute assignment, or an element of a list or tuple of either)
in its body or decorators, and flags it when the function has no assertion:
no ``assert`` statement, no call to an ``assert*`` function or method, and no
``pytest.raises`` / ``pytest.warns`` / ``pytest.fail``.
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

_UNIT_ROOT = Path(__file__).parent

#: A class name that reads as an exception type.
_EXCEPTION_NAME = re.compile(
    r"^(?:BaseException|Exception)$|(?:Error|Exception|Exit|Interrupt|Timeout)$"
)

#: ``pytest`` helpers that assert by raising or by expecting a raise.
_PYTEST_ASSERTS = frozenset({"raises", "warns", "fail", "deprecated_call"})


@dataclass(frozen=True)
class _Finding:
    path: str
    line: int
    name: str

    def __str__(self) -> str:
        return f"tests/unit/{self.path}:{self.line}: {self.name}"


def _terminal_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_exception(node: ast.AST) -> bool:
    """``RuntimeError``, ``RuntimeError("x")``, ``httpx.ConnectError(...)``,
    or a list or tuple holding one (a side effect that fails on some call)."""
    if isinstance(node, ast.List | ast.Tuple):
        return any(_is_exception(elt) for elt in node.elts)
    if isinstance(node, ast.Call):
        node = node.func
    return bool(_EXCEPTION_NAME.search(_terminal_name(node)))


def _injects_a_failure(nodes: list[ast.AST]) -> bool:
    for node in nodes:
        if (
            isinstance(node, ast.keyword)
            and node.arg == "side_effect"
            and _is_exception(node.value)
        ):
            return True
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "side_effect"
                for t in node.targets
            )
            and _is_exception(node.value)
        ):
            return True
    return False


def _asserts_something(nodes: list[ast.AST]) -> bool:
    for node in nodes:
        if isinstance(node, ast.Assert):
            return True
        if not isinstance(node, ast.Call):
            continue
        name = _terminal_name(node.func)
        if name.lstrip("_").startswith("assert"):
            return True
        if (
            name in _PYTEST_ASSERTS
            and isinstance(node.func, ast.Attribute)
            and _terminal_name(node.func.value) == "pytest"
        ):
            return True
    return False


def _scan(path: Path, root: Path = _UNIT_ROOT) -> list[_Finding]:
    source = path.read_text(encoding="utf-8")
    if "side_effect" not in source:
        return []
    rel = path.relative_to(root).as_posix()
    findings = []
    for fn in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not fn.name.startswith("test"):
            continue
        body = [n for stmt in fn.body for n in ast.walk(stmt)]
        decorators = [n for d in fn.decorator_list for n in ast.walk(d)]
        if _injects_a_failure(body + decorators) and not _asserts_something(body):
            findings.append(_Finding(rel, fn.lineno, fn.name))
    return findings


def _test_modules() -> list[Path]:
    this = Path(__file__).resolve()
    return sorted(p for p in _UNIT_ROOT.rglob("test_*.py") if p.resolve() != this)


@functools.cache
def _findings() -> list[_Finding]:
    return [f for path in _test_modules() for f in _scan(path)]


def test_no_unit_test_injects_a_failure_and_asserts_nothing() -> None:
    findings = _findings()
    assert not findings, (
        "These tests inject an exception side_effect and assert nothing, so "
        "they pass when the failing call is never reached. Assert the mock "
        "was called (and the log line, if the failure is logged and "
        "swallowed); see this module's docstring:\n"
        + "\n".join(str(f) for f in findings)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        # Keyword on a patch, inside the body.
        "def test_x():\n"
        "    with patch.object(m, 'f', side_effect=RuntimeError('boom')):\n"
        "        run()\n",
        # Attribute assignment.
        "def test_x():\n    m.f.side_effect = OSError('x')\n    run()\n",
        # A bare exception class.
        "def test_x():\n    m.side_effect = ValueError\n    run()\n",
        # A qualified one.
        "async def test_x():\n"
        "    get = AsyncMock(side_effect=httpx.ConnectError('refused'))\n"
        "    await run(get)\n",
        # Fails on the second call only.
        "def test_x():\n    m.side_effect = [None, TimeoutError()]\n    run()\n",
        # Injected in a decorator.
        "@patch('a.b', side_effect=Exception('x'))\ndef test_x(b):\n    run()\n",
        # A method on a test class.
        "class TestX:\n"
        "    def test_x(self):\n"
        "        m.side_effect = KeyboardInterrupt\n"
        "        run()\n",
        # A "must not be called" probe that forgets assert_not_called.
        "def test_x():\n"
        "    with patch.object(m, 'f', side_effect=AssertionError('called')):\n"
        "        run()\n",
    ],
)
def test_the_scan_catches_each_shape(tmp_path: Path, snippet: str) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(snippet, encoding="utf-8")
    assert _scan(probe, tmp_path), f"missed: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "def test_x():\n"
        "    with patch.object(m, 'f', side_effect=RuntimeError('x')) as f:\n"
        "        run()\n"
        "    f.assert_called_once()\n",
        "async def test_x():\n"
        "    m.side_effect = OSError('x')\n"
        "    await run()\n"
        "    m.assert_awaited_once_with()\n",
        "def test_x():\n    m.side_effect = OSError('x')\n    assert run() is None\n",
        "def test_x():\n"
        "    m.side_effect = OSError('x')\n"
        "    with pytest.raises(OSError):\n"
        "        run()\n",
        "def test_x():\n    m.side_effect = OSError('x')\n    run()\n    _assert_logged()\n",
        # A non-exception side effect is not fault injection.
        "def test_x():\n    m.side_effect = fake_sleep\n    run()\n",
        "def test_x():\n    m.side_effect = [1, 2, 3]\n    run()\n",
        # Not a test function.
        "def helper():\n    m.side_effect = OSError('x')\n    run()\n",
    ],
)
def test_the_scan_leaves_asserting_tests_alone(tmp_path: Path, snippet: str) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(snippet, encoding="utf-8")
    assert not _scan(probe, tmp_path), f"flagged: {snippet!r}"
