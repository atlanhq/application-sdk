"""Meta-tests for the preflight-gate checks (P032–P035, BLDX-1545).

These checks fan out across the fleet, so each rule is tested to fire *exactly*
when it should and stay silent otherwise — both false positives and false
negatives are guarded, plus the inline-suppression path.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.preflight import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_HANDLER_IMPORTS = (
    "from application_sdk.handler.base import Handler\n"
    "from application_sdk.handler.contracts import "
    "PreflightCheck, PreflightInput, PreflightOutput\n"
)
_APP_IMPORTS = (
    "from application_sdk.app import App, task, entrypoint\n"
    "from application_sdk.contracts import Input\n"
    "from pydantic import Field\n"
)


def _scan(tmp_path: Path, files: dict[str, str]) -> list:
    paths: list[Path] = []
    for name, src in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
        paths.append(p)
    return scan_all(paths, tmp_path)


def _ids(tmp_path: Path, src: str) -> list[str]:
    return sorted(f.rule_id for f in _scan(tmp_path, {"m.py": src}))


# ── metadata ────────────────────────────────────────────────────────────────


def test_rule_metadata() -> None:
    for rid in ("P032", "P033", "P034", "P035"):
        rule = get_rule(rid)
        assert rule.scope is RuleScope.APP
        assert rule.tier is EnforcementTier.WARN
        assert rule.mechanism is RuleMechanism.STATIC


# ── P032 ReservedPreflightActivityName ────────────────────────────────────────


def test_p032_fires_on_explicit_name(tmp_path: Path) -> None:
    src = (
        _APP_IMPORTS
        + "class A(App):\n"
        + '    @task(name="preflight")\n'
        + "    async def anything(self): ...\n"
    )
    assert _ids(tmp_path, src) == ["P032"]


def test_p032_fires_on_bare_task_named_preflight(tmp_path: Path) -> None:
    src = (
        _APP_IMPORTS
        + "class A(App):\n"
        + "    @task\n"
        + "    async def preflight(self): ...\n"
    )
    assert _ids(tmp_path, src) == ["P032"]


def test_p032_silent_on_non_preflight_task(tmp_path: Path) -> None:
    src = _APP_IMPORTS + "class A(App):\n    @task\n    async def discover(self): ...\n"
    assert _ids(tmp_path, src) == []


def test_p032_silent_on_non_sdk_task(tmp_path: Path) -> None:
    src = (
        "from celery import task\n"
        "class A:\n"
        "    @task\n"
        "    async def preflight(self): ...\n"
    )
    assert _ids(tmp_path, src) == []


def test_p032_silent_on_non_literal_name(tmp_path: Path) -> None:
    src = (
        _APP_IMPORTS
        + "NAME = 'preflight'\n"
        + "class A(App):\n"
        + "    @task(name=NAME)\n"
        + "    async def anything(self): ...\n"
    )
    assert _ids(tmp_path, src) == []


def test_p032_fires_on_aliased_sdk_task_import(tmp_path: Path) -> None:
    src = (
        "from application_sdk.app import App, task as t\n"
        "class A(App):\n"
        '    @t(name="preflight")\n'
        "    async def anything(self): ...\n"
    )
    assert _ids(tmp_path, src) == ["P032"]


def test_p032_suppressed(tmp_path: Path) -> None:
    src = (
        _APP_IMPORTS
        + "class A(App):\n"
        + '    @task(name="preflight")\n'
        + "    # conformance: ignore[P032] legacy task, migration tracked\n"
        + "    async def anything(self): ...\n"
    )
    findings = _scan(tmp_path, {"m.py": src})
    assert [f.rule_id for f in findings] == ["P032"]
    assert findings[0].suppressed is True


# ── P033 DuplicateInWorkflowPreflight ──────────────────────────────────────────


def _handler_with_preflight(
    body: str = "        return PreflightOutput(checks=[])\n",
) -> str:
    return (
        _HANDLER_IMPORTS
        + "class H(Handler):\n"
        + "    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + body
    )


def test_p033_fires_when_handler_and_preflight_task_coexist(tmp_path: Path) -> None:
    app = (
        _APP_IMPORTS
        + "class A(App):\n"
        + "    @task\n"
        + "    async def run_preflight(self): ...\n"
    )
    ids = sorted(
        f.rule_id
        for f in _scan(tmp_path, {"app.py": app, "h.py": _handler_with_preflight()})
    )
    assert ids == ["P033"]


def test_p033_silent_without_handler_preflight(tmp_path: Path) -> None:
    src = (
        _APP_IMPORTS
        + "class A(App):\n    @task\n    async def run_preflight(self): ...\n"
    )
    assert _ids(tmp_path, src) == []


def test_p033_silent_on_exact_reserved_name_that_is_p032(tmp_path: Path) -> None:
    app = (
        _APP_IMPORTS + "class A(App):\n    @task\n    async def preflight(self): ...\n"
    )
    ids = sorted(
        f.rule_id
        for f in _scan(tmp_path, {"app.py": app, "h.py": _handler_with_preflight()})
    )
    assert ids == ["P032"]  # never double-fires P033


def test_p033_silent_on_preflight_substring_non_token(tmp_path: Path) -> None:
    app = (
        _APP_IMPORTS
        + "class A(App):\n    @task\n    async def preflightation(self): ...\n"
    )
    ids = sorted(
        f.rule_id
        for f in _scan(tmp_path, {"app.py": app, "h.py": _handler_with_preflight()})
    )
    assert ids == []


def test_p033_fires_via_transitive_handler_without_preflight_input_annotation(
    tmp_path: Path,
) -> None:
    # Handler detected only through the transitive base chain (in-repo intermediate),
    # with an un-annotated preflight_check — exercises _class_subclasses_handler.
    handler = (
        "from application_sdk.handler.base import Handler, DefaultHandler\n"
        "class Base(DefaultHandler): ...\n"
        "class Concrete(Base):\n"
        "    async def preflight_check(self, request):\n"
        "        return None\n"
    )
    app = (
        _APP_IMPORTS
        + "class A(App):\n    @task\n    async def run_preflight(self): ...\n"
    )
    ids = sorted(f.rule_id for f in _scan(tmp_path, {"app.py": app, "h.py": handler}))
    assert ids == ["P033"]


def test_p033_suppressed(tmp_path: Path) -> None:
    app = (
        _APP_IMPORTS
        + "class A(App):\n"
        + "    @task\n"
        + "    # conformance: ignore[P033] kept intentionally, see TICKET-1\n"
        + "    async def run_preflight(self): ...\n"
    )
    findings = _scan(tmp_path, {"app.py": app, "h.py": _handler_with_preflight()})
    assert [f.rule_id for f in findings] == ["P033"]
    assert findings[0].suppressed is True


# ── P034 UntypedPreflightCheckFailure ──────────────────────────────────────────


def _pc(expr: str) -> str:
    return (
        "from application_sdk.handler.contracts import PreflightCheck\n"
        "def make():\n"
        f"    return {expr}\n"
    )


def test_p034_fires_on_explicit_passed_false(tmp_path: Path) -> None:
    assert _ids(tmp_path, _pc('PreflightCheck(name="x", passed=False)')) == ["P034"]


def test_p034_fires_with_only_deprecated_message(tmp_path: Path) -> None:
    assert _ids(
        tmp_path, _pc('PreflightCheck(name="x", passed=False, message="boom")')
    ) == ["P034"]


def test_p034_fires_on_explicit_error_none(tmp_path: Path) -> None:
    assert _ids(
        tmp_path, _pc('PreflightCheck(name="x", passed=False, error=None)')
    ) == ["P034"]


def test_p034_silent_with_typed_error(tmp_path: Path) -> None:
    src = (
        "from application_sdk.handler.contracts import PreflightCheck\n"
        "def make(err):\n"
        '    return PreflightCheck(name="x", passed=False, error=err)\n'
    )
    assert _ids(tmp_path, src) == []


def test_p034_silent_on_passed_true(tmp_path: Path) -> None:
    assert _ids(tmp_path, _pc('PreflightCheck(name="x", passed=True)')) == []


def test_p034_silent_on_omitted_passed(tmp_path: Path) -> None:
    # Deliberate false-negative: bare templates are the biggest FP source.
    assert _ids(tmp_path, _pc('PreflightCheck(name="x")')) == []


def test_p034_silent_on_non_literal_passed(tmp_path: Path) -> None:
    src = (
        "from application_sdk.handler.contracts import PreflightCheck\n"
        "def make(ok):\n"
        '    return PreflightCheck(name="x", passed=ok)\n'
    )
    assert _ids(tmp_path, src) == []


def test_p034_silent_on_non_sdk_preflightcheck(tmp_path: Path) -> None:
    src = (
        "class PreflightCheck:\n    pass\n"
        "def make():\n"
        '    return PreflightCheck(name="x", passed=False)\n'
    )
    assert _ids(tmp_path, src) == []


def test_p034_fires_via_module_alias_call(tmp_path: Path) -> None:
    src = (
        "import application_sdk.handler.contracts as c\n"
        "def make():\n"
        '    return c.PreflightCheck(name="x", passed=False)\n'
    )
    assert _ids(tmp_path, src) == ["P034"]


def test_p034_suppressed(tmp_path: Path) -> None:
    src = (
        "from application_sdk.handler.contracts import PreflightCheck\n"
        "def make():\n"
        "    # conformance: ignore[P034] migrating to typed errors\n"
        '    return PreflightCheck(name="x", passed=False)\n'
    )
    findings = _scan(tmp_path, {"m.py": src})
    assert [f.rule_id for f in findings] == ["P034"]
    assert findings[0].suppressed is True


# ── P035 PreflightMetadataContractParity ───────────────────────────────────────


def _app_with_input(fields: str) -> str:
    return (
        _APP_IMPORTS
        + "class ExtractInput(Input):\n"
        + fields
        + "class A(App):\n"
        + "    @entrypoint()\n"
        + "    async def extract(self, input: ExtractInput) -> None: ...\n"
    )


def _handler_reading(*reads: str) -> str:
    body = "".join(f"        {r}\n" for r in reads)
    return (
        _HANDLER_IMPORTS
        + "class H(Handler):\n"
        + "    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + body
        + "        return PreflightOutput(checks=[])\n"
    )


def test_p035_fires_on_key_absent_from_contract(tmp_path: Path) -> None:
    files = {
        "app.py": _app_with_input("    include_filter: dict = {}\n"),
        "h.py": _handler_reading('x = input.metadata.get("unknown_key")'),
    }
    assert sorted(f.rule_id for f in _scan(tmp_path, files)) == ["P035"]


def test_p035_silent_on_declared_field(tmp_path: Path) -> None:
    files = {
        "app.py": _app_with_input("    include_filter: dict = {}\n"),
        "h.py": _handler_reading('x = input.metadata.get("include_filter")'),
    }
    assert [f.rule_id for f in _scan(tmp_path, files)] == []


def test_p035_silent_on_hyphenated_alias_of_field(tmp_path: Path) -> None:
    files = {
        "app.py": _app_with_input("    include_filter: dict = {}\n"),
        "h.py": _handler_reading('x = input.metadata.get("include-filter")'),
    }
    assert [f.rule_id for f in _scan(tmp_path, files)] == []


def test_p035_fires_on_differently_stemmed_alias(tmp_path: Path) -> None:
    # model_dump emits field NAMES, not aliases, so a read via a differently-stemmed
    # alias genuinely misses on the gate path and must fire.
    files = {
        "app.py": _app_with_input(
            '    object_filter: dict = Field(default_factory=dict, alias="type")\n'
        ),
        "h.py": _handler_reading('x = input.metadata.get("type")'),
    }
    assert sorted(f.rule_id for f in _scan(tmp_path, files)) == ["P035"]


def test_p035_silent_on_subscript_read_of_field(tmp_path: Path) -> None:
    files = {
        "app.py": _app_with_input("    include_filter: dict = {}\n"),
        "h.py": _handler_reading('x = input.metadata["include_filter"]'),
    }
    assert [f.rule_id for f in _scan(tmp_path, files)] == []


def test_p035_silent_on_dynamic_key(tmp_path: Path) -> None:
    files = {
        "app.py": _app_with_input("    include_filter: dict = {}\n"),
        "h.py": _handler_reading("k = 'x'", "y = input.metadata.get(k)"),
    }
    assert [f.rule_id for f in _scan(tmp_path, files)] == []


def test_p035_silent_when_contract_allows_extra_keys(tmp_path: Path) -> None:
    # model_config extra="allow" genuinely keeps undeclared keys → no parity claim.
    app = (
        _APP_IMPORTS
        + "from pydantic import ConfigDict\n"
        + "class ExtractInput(Input):\n"
        + '    model_config = ConfigDict(extra="allow")\n'
        + "    include_filter: dict = {}\n"
        + "class A(App):\n"
        + "    @entrypoint()\n"
        + "    async def extract(self, input: ExtractInput) -> None: ...\n"
    )
    files = {
        "app.py": app,
        "h.py": _handler_reading('x = input.metadata.get("anything")'),
    }
    assert [f.rule_id for f in _scan(tmp_path, files)] == []


def test_p035_fires_despite_allow_unbounded_fields(tmp_path: Path) -> None:
    # allow_unbounded_fields only skips payload-safety type checks; the extra policy
    # stays "ignore", so undeclared keys are still dropped and P035 must still fire.
    app = (
        _APP_IMPORTS
        + "class ExtractInput(Input, allow_unbounded_fields=True):\n"
        + "    include_filter: dict = {}\n"
        + "class A(App):\n"
        + "    @entrypoint()\n"
        + "    async def extract(self, input: ExtractInput) -> None: ...\n"
    )
    files = {
        "app.py": app,
        "h.py": _handler_reading('x = input.metadata.get("unknown_key")'),
    }
    assert sorted(f.rule_id for f in _scan(tmp_path, files)) == ["P035"]


def test_p035_silent_when_no_entrypoint_input(tmp_path: Path) -> None:
    # No entrypoint Input contract to compare against → cannot make a parity claim.
    assert [
        f.rule_id
        for f in _scan(
            tmp_path, {"h.py": _handler_reading('x = input.metadata.get("anything")')}
        )
    ] == []


def test_p035_suppressed(tmp_path: Path) -> None:
    files = {
        "app.py": _app_with_input("    include_filter: dict = {}\n"),
        "h.py": _handler_reading(
            "# conformance: ignore[P035] form-only key, tracked",
            'x = input.metadata.get("unknown_key")',
        ),
    }
    findings = _scan(tmp_path, files)
    assert [f.rule_id for f in findings] == ["P035"]
    assert findings[0].suppressed is True
