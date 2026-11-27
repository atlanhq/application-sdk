"""P069 reach: workflow code that calls into this repo's helpers.

The motivating failure was an ``@entrypoint`` delegating to an async
``run_*_flow()`` in another module, whose import ran on the workflow thread.
These tests guard that the walk reaches it, follows sync and async helpers
transitively, stops at ``@task`` and third-party code, terminates on cycles,
and honours a suppression on the import inside the helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.suite.checks.determinism import scan_path, scan_text

_HEADER = "from application_sdk.app import App, task, entrypoint\n"


def _p069(source: str) -> list:
    return [f for f in scan_text(source, "app/x.py") if f.rule_id == "P069"]


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _p069_at(path: Path, root: Path) -> list:
    return [f for f in scan_path(path, root) if f.rule_id == "P069"]


_FLOW = (
    "async def run_extract_flow(input):\n"
    "    return await _stage(input)\n"
    "async def _stage(input):\n"
    "    from app.extract.marker import build_prefix\n"
    "    return build_prefix(input)\n"
)


@pytest.mark.parametrize(
    ("import_line", "call"),
    [
        ("from app.flows import run_extract_flow", "run_extract_flow(input)"),
        ("from .flows import run_extract_flow", "run_extract_flow(input)"),
        ("from app import flows", "flows.run_extract_flow(input)"),
        ("import app.flows as flows", "flows.run_extract_flow(input)"),
    ],
)
def test_entrypoint_delegating_to_a_flow_in_another_module(
    tmp_path: Path, import_line: str, call: str
) -> None:
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/flows.py", _FLOW)
    caller = _write(
        tmp_path,
        "app/connector.py",
        f"{import_line}\n{_HEADER}"
        "class C(App):\n"
        "    @entrypoint\n"
        "    async def extract(self, input):\n"
        f"        return await {call}\n",
    )
    (finding,) = _p069_at(caller, tmp_path)
    assert finding.file == "app/connector.py"
    assert finding.line == 6
    assert "run_extract_flow() -> _stage()" in finding.message
    assert "'app.extract.marker' at app/flows.py:4" in finding.message


def test_same_module_helper_and_self_method() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    import json\n"
        "    return json.dumps(x)\n"
        "class C(App):\n"
        "    def _build(self, x):\n"
        "        return helper(x)\n"
        "    async def run(self, input):\n"
        "        return self._build(input)\n"
    )
    (finding,) = _p069(src)
    assert finding.line == 9
    assert "_build() -> helper()" in finding.message
    assert "app/x.py:3" in finding.message


def test_task_method_is_not_followed() -> None:
    src = _HEADER + (
        "class C(App):\n"
        "    @task\n"
        "    async def fetch(self, input):\n"
        "        import pyarrow\n"
        "        return pyarrow\n"
        "    async def run(self, input):\n"
        "        return await self.fetch(input)\n"
    )
    assert _p069(src) == []


def test_other_workflow_method_is_not_reported_twice() -> None:
    src = _HEADER + (
        "class C(App):\n"
        "    @entrypoint\n"
        "    async def a(self, input):\n"
        "        import json\n"
        "    async def run(self, input):\n"
        "        return await self.a(input)\n"
    )
    assert [f.line for f in _p069(src)] == [5]


def test_third_party_and_unresolvable_calls_are_not_followed(tmp_path: Path) -> None:
    caller = _write(
        tmp_path,
        "app/connector.py",
        f"from somelib.flows import run_flow\nimport somelib\n{_HEADER}"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        await run_flow(input)\n"
        "        somelib.go(input)\n"
        "        self.inherited(input)\n",
    )
    assert _p069_at(caller, tmp_path) == []


def test_helper_without_imports_is_silent() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    return x\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        return helper(input)\n"
    )
    assert _p069(src) == []


def test_call_cycle_terminates_and_still_reaches_the_import() -> None:
    src = _HEADER + (
        "def a(x):\n"
        "    return b(x)\n"
        "def b(x):\n"
        "    if x:\n"
        "        return a(x - 1)\n"
        "    import json\n"
        "    return json\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        return a(input)\n"
    )
    (finding,) = _p069(src)
    assert "a() -> b()" in finding.message


def test_suppression_on_the_helper_import_waives_every_caller() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    import json  # conformance: ignore[P069] reviewed: stdlib, passed through\n"
        "    return json.dumps(x)\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        return helper(input)\n"
        "    @entrypoint\n"
        "    async def other(self, input):\n"
        "        return helper(input)\n"
    )
    assert _p069(src) == []


def test_suppression_at_the_call_site() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    import json\n"
        "    return json\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        return helper(input)  # conformance: ignore[P069] reviewed\n"
    )
    (finding,) = _p069(src)
    assert finding.suppressed


def test_type_checking_import_in_helper_is_silent() -> None:
    src = _HEADER + (
        "from typing import TYPE_CHECKING\n"
        "def helper(x):\n"
        "    if TYPE_CHECKING:\n"
        "        from app.models import Meta\n"
        "    return x\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        return helper(input)\n"
    )
    assert _p069(src) == []


def test_helper_outside_an_app_module_is_not_a_root(tmp_path: Path) -> None:
    # A helper module is only reported through a workflow that reaches it.
    _write(tmp_path, "app/__init__.py", "")
    flows = _write(tmp_path, "app/flows.py", _FLOW)
    assert _p069_at(flows, tmp_path) == []


# ── TYPE_CHECKING else branches (lens F-109e36) ──────────────────────────────


def test_type_checking_else_branch_import_is_flagged() -> None:
    src = _HEADER + (
        "from typing import TYPE_CHECKING\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        if TYPE_CHECKING:\n"
        "            from app.models import Meta\n"
        "        else:\n"
        "            import json\n"
        "        return input\n"
    )
    (finding,) = _p069(src)
    assert finding.line == 8
    assert "'json'" in finding.message


def test_type_checking_elif_branch_call_is_followed() -> None:
    src = _HEADER + (
        "import typing\n"
        "def helper(x):\n"
        "    import json\n"
        "    return json\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        if typing.TYPE_CHECKING:\n"
        "            pass\n"
        "        elif input:\n"
        "            return helper(input)\n"
    )
    (finding,) = _p069(src)
    assert finding.line == 11
    assert "helper()" in finding.message


# ── shadowed names (lens F-ac8ead, F-72cd23) ─────────────────────────────────


_SHADOWED_HELPER = (
    "def helper(x):\n"
    "    import json\n"
    "    return json\n"
    "class C(App):\n"
    "    async def run(self, input):\n"
)


@pytest.mark.parametrize(
    "body",
    [
        "        helper = lambda x: x\n        return helper(input)\n",
        "        def helper(x):\n            return x\n        return helper(input)\n",
        "        from app.other import helper\n        return helper(input)\n",
        "        for helper in input:\n            helper(input)\n",
    ],
    ids=["assignment", "nested-def", "local-import", "loop-target"],
)
def test_locally_bound_name_shadows_the_module_helper(body: str) -> None:
    src = _HEADER + _SHADOWED_HELPER + body
    # A local import of `helper` is itself a direct finding; nothing is reached.
    assert all("reaches" not in f.message for f in _p069(src))


def test_parameter_shadows_the_module_helper() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    import json\n"
        "    return json\n"
        "class C(App):\n"
        "    async def run(self, helper):\n"
        "        return helper(1)\n"
    )
    assert _p069(src) == []


def test_shadowed_imported_helper_is_not_followed(tmp_path: Path) -> None:
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/flows.py", _FLOW)
    caller = _write(
        tmp_path,
        "app/connector.py",
        f"from app.flows import run_extract_flow\nfrom app import flows\n{_HEADER}"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        run_extract_flow = lambda x: x\n"
        "        flows = input\n"
        "        run_extract_flow(input)\n"
        "        return flows.run_extract_flow(input)\n",
    )
    assert _p069_at(caller, tmp_path) == []


def test_global_declaration_does_not_shadow() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    import json\n"
        "    return json\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        global helper\n"
        "        return helper(input)\n"
    )
    (finding,) = _p069(src)
    assert "helper()" in finding.message


def test_nested_function_sees_enclosing_shadowing() -> None:
    src = _HEADER + (
        "def helper(x):\n"
        "    import json\n"
        "    return json\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        helper = str\n"
        "        def inner():\n"
        "            return helper(input)\n"
        "        return inner()\n"
    )
    assert _p069(src) == []


def test_shadowing_in_a_reached_helper_stops_the_walk() -> None:
    src = _HEADER + (
        "def deep(x):\n"
        "    import json\n"
        "    return json\n"
        "def middle(x):\n"
        "    deep = str\n"
        "    return deep(x)\n"
        "class C(App):\n"
        "    async def run(self, input):\n"
        "        return middle(input)\n"
    )
    assert _p069(src) == []
