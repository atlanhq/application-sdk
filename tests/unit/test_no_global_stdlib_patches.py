"""No unit test patches a process-global stdlib function.

An xdist worker runs one test at a time, but not one *thread*: ``run_in_thread``
offloads, the loguru and observability flushes, and the event loop's own timers
from earlier tests are still running. ``patch("application_sdk.X.time.monotonic")``
patches the one global ``time`` module, so all of them see the fake clock, and
``patch("asyncio.create_task")`` captures every task the loop creates, including
library internals. Patch a private copy instead: :mod:`tests.unit.private_stdlib`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_UNIT_ROOT = Path(__file__).parent
_EXEMPT = {Path(__file__).name, "private_stdlib.py"}

#: The stdlib modules whose functions SDK code and the event loop share. ``sys``
#: is only flagged when reached through an SDK module (``sys.platform``):
#: ``monkeypatch.setattr(sys, "argv", ...)`` is the standard way to drive a CLI.
_SHARED = ("time", "asyncio", "os", "sys")
_SHARED_BY_NAME = ("time", "asyncio", "os")

#: ``"application_sdk.main.time.monotonic"`` and friends.
_SDK_PATH = re.compile(rf"application_sdk(\.\w+)+\.({'|'.join(_SHARED)})(\.\w+)+")
#: ``"asyncio.create_task"``, ``"time.monotonic"``.
_BARE_PATH = re.compile(r"(time|asyncio)(\.\w+)+")


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _violation(node: ast.Call) -> str | None:
    name = _callee(node)
    if name not in {"patch", "object", "setattr"} or not node.args:
        return None
    target = node.args[0]
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        if name == "object":
            return None
        if _SDK_PATH.fullmatch(target.value) or _BARE_PATH.fullmatch(target.value):
            return target.value
        return None
    if name not in {"object", "setattr"}:
        return None
    if isinstance(target, ast.Name) and target.id in _SHARED_BY_NAME:
        return target.id
    if isinstance(target, ast.Attribute) and target.attr in _SHARED:
        return ast.unparse(target)
    return None


def test_no_unit_test_patches_a_global_stdlib_function() -> None:
    found: list[str] = []
    for path in sorted(_UNIT_ROOT.rglob("*.py")):
        if path.name in _EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (target := _violation(node)):
                found.append(f"{path.relative_to(_UNIT_ROOT)}:{node.lineno}: {target}")
    assert not found, (
        "These patch a stdlib module shared by every thread on the xdist worker; "
        "patch a private copy via tests.unit.private_stdlib instead:\n"
        + "\n".join(found)
    )


def test_the_guard_catches_each_global_form() -> None:
    sources = {
        'patch("application_sdk.main.time.monotonic")': True,
        'monkeypatch.setattr("application_sdk.x.asyncio.sleep", f)': True,
        'patch("asyncio.create_task")': True,
        'patch.object(mod.asyncio, "sleep")': True,
        'patch.object(os, "replace")': True,
        'monkeypatch.setattr(mod.sys, "platform", "win32")': True,
        'patch.object(local_time, "monotonic")': False,
        'monkeypatch.setattr(sys, "argv", [])': False,
        'patch("application_sdk.main.run")': False,
        'patch("os.path.isfile")': False,
    }
    for source, flagged in sources.items():
        call = ast.parse(source).body[0].value  # type: ignore[attr-defined]  # — every source above is a single expression statement
        assert (_violation(call) is not None) is flagged, source
