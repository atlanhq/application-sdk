"""Meta-tests for the preflight-gate checks (P032–P035, P047, BLDX-1545, FND-901).

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
    for rid in ("P033", "P034", "P035", "P047"):
        rule = get_rule(rid)
        assert rule.scope is RuleScope.APP
        assert rule.tier is EnforcementTier.WARN
        assert rule.mechanism is RuleMechanism.STATIC


def test_p032_is_block_tier() -> None:
    """P032 is the one BLOCK-tier preflight rule (FND-311).

    Its siblings describe preflight *quality* — drift, UX, parity — which
    degrades messages. P032 describes a worker that never boots, so every
    workflow in the tenant is down from the moment the release deploys.
    """
    rule = get_rule("P032")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.BLOCK
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


def test_p033_message_points_at_colocated_handler(tmp_path: Path) -> None:
    # With more than one preflight_check in scope, the message must reference the
    # handler in the task's own source, not whichever site was scanned first.
    other = _handler_with_preflight()  # a separate handler, another file
    colocated = (
        _APP_IMPORTS
        + _HANDLER_IMPORTS
        + "class A(App):\n"
        + "    @task\n"
        + "    async def run_preflight(self): ...\n"
        + "class H(Handler):\n"
        + "    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + "        return PreflightOutput(checks=[])\n"
    )
    findings = _scan(tmp_path, {"other.py": other, "app.py": colocated})
    p033 = [f for f in findings if f.rule_id == "P033"]
    assert len(p033) == 1
    assert "app.py:" in p033[0].message
    assert "other.py:" not in p033[0].message


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


def test_p034_silent_on_kwargs_expansion(tmp_path: Path) -> None:
    # A ``**`` expansion may carry a typed error=; suppress rather than false-fire.
    src = (
        "from application_sdk.handler.contracts import PreflightCheck\n"
        "def make(err):\n"
        '    return PreflightCheck(name="x", passed=False, **err)\n'
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


def test_p035_silent_on_dict_literal_extra_allow(tmp_path: Path) -> None:
    # The pydantic v2 dict-literal form opts into extras just like ConfigDict → no parity claim.
    app = (
        _APP_IMPORTS
        + "class ExtractInput(Input):\n"
        + '    model_config = {"extra": "allow"}\n'
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


# ── P047 PreflightFailureLoggedAsWarning ───────────────────────────────────────


def test_p047_fires_on_logger_warning(tmp_path: Path) -> None:
    src = _handler_with_preflight(
        '        logger.warning("auth check failed: %s", "boom")\n'
        "        return PreflightOutput(checks=[])\n"
    )
    assert [f.rule_id for f in _scan(tmp_path, {"h.py": src})] == ["P047"]


def test_p047_fires_on_self_logger_and_stdlib_logging(tmp_path: Path) -> None:
    src = _handler_with_preflight(
        '        self.logger.warning("degraded")\n'
        '        logging.warning("degraded")\n'
        "        return PreflightOutput(checks=[])\n"
    )
    assert [f.rule_id for f in _scan(tmp_path, {"h.py": src})] == ["P047", "P047"]


def test_p047_silent_on_other_levels(tmp_path: Path) -> None:
    src = _handler_with_preflight(
        '        logger.info("probing")\n'
        '        logger.error("probe crashed", exc_info=True)\n'
        "        return PreflightOutput(checks=[])\n"
    )
    assert [f.rule_id for f in _scan(tmp_path, {"h.py": src})] == []


def test_p047_silent_outside_preflight_check(tmp_path: Path) -> None:
    src = (
        _HANDLER_IMPORTS
        + "class H(Handler):\n"
        + "    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + "        return PreflightOutput(checks=[])\n"
        + "    async def other(self):\n"
        + '        logger.warning("fine here")\n'
    )
    assert [f.rule_id for f in _scan(tmp_path, {"h.py": src})] == []


def test_p047_fires_on_common_logger_aliases(tmp_path: Path) -> None:
    src = _handler_with_preflight(
        '        log.warning("via log alias")\n'
        '        self._log.warning("via private attr")\n'
        '        logger.warn("deprecated alias")\n'
        "        return PreflightOutput(checks=[])\n"
    )
    assert [f.rule_id for f in _scan(tmp_path, {"h.py": src})] == ["P047"] * 3


def test_p047_silent_on_non_logger_receiver(tmp_path: Path) -> None:
    src = _handler_with_preflight(
        '        warnings.warning("not a logger")\n'
        '        self._client.warning("a source API, not a logger")\n'
        "        return PreflightOutput(checks=[])\n"
    )
    assert [f.rule_id for f in _scan(tmp_path, {"h.py": src})] == []


def test_p047_suppressed(tmp_path: Path) -> None:
    src = _handler_with_preflight(
        "        # conformance: ignore[P047] advisory-only probe, see FND-901\n"
        '        logger.warning("advisory")\n'
        "        return PreflightOutput(checks=[])\n"
    )
    findings = _scan(tmp_path, {"h.py": src})
    assert [f.rule_id for f in findings] == ["P047"]
    assert findings[0].suppressed is True


# ── P041 GateBrokenCategoryUserAudience ───────────────────────────────────────

_ERR_IMPORTS = (
    "from typing import ClassVar\n"
    "from application_sdk.errors.base import AppError\n"
    "from application_sdk.errors.wire import Audience, FailureCategory\n"
)


def _leaf(body: str, *, name: str = "Boom", base: str = "AppError") -> str:
    return f"{_ERR_IMPORTS}\n\nclass {name}({base}):\n{body}"


def test_p041_rule_metadata() -> None:
    """P041 is BOTH-scoped: the defect exists in the SDK's own leaves too."""
    rule = get_rule("P041")
    assert rule.scope is RuleScope.BOTH
    assert rule.tier is EnforcementTier.WARN
    assert rule.mechanism is RuleMechanism.STATIC


def test_p041_fires_on_declared_gate_broken_category_with_user_audience(
    tmp_path: Path,
) -> None:
    src = _leaf(
        "    category: ClassVar[FailureCategory] = FailureCategory.RATE_LIMITED\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    assert _ids(tmp_path, src) == ["P041"]


def test_p041_fires_on_each_gate_broken_category(tmp_path: Path) -> None:
    for cat in (
        "DEPENDENCY_UNAVAILABLE",
        "RATE_LIMITED",
        "RESOURCE_EXHAUSTED",
        "CANCELLED",
    ):
        src = _leaf(
            f"    category: ClassVar[FailureCategory] = FailureCategory.{cat}\n"
            "    audience: ClassVar[Audience] = Audience.USER\n"
        )
        assert _ids(tmp_path / cat, src) == ["P041"], cat


def test_p041_fires_on_subclass_inheriting_category_from_sdk_leaf(
    tmp_path: Path,
) -> None:
    """An app subclass inherits RATE_LIMITED and re-blames the customer."""
    src = (
        "from typing import ClassVar\n"
        "from application_sdk.errors.leaves import RateLimitedError\n"
        "from application_sdk.errors.wire import Audience\n"
        "\n\nclass SourceThrottled(RateLimitedError):\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    assert _ids(tmp_path, src) == ["P041"]


def test_p041_fires_through_an_in_file_intermediate(tmp_path: Path) -> None:
    src = (
        "from typing import ClassVar\n"
        "from application_sdk.errors.leaves import RateLimitedError\n"
        "from application_sdk.errors.wire import Audience\n"
        "\n\nclass Mid(RateLimitedError):\n    pass\n"
        "\n\nclass Leaf(Mid):\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    assert _ids(tmp_path, src) == ["P041"]


def test_p041_silent_on_correct_audience(tmp_path: Path) -> None:
    for aud in ("APP_OWNER", "PLATFORM"):
        src = _leaf(
            "    category: ClassVar[FailureCategory] = FailureCategory.RATE_LIMITED\n"
            f"    audience: ClassVar[Audience] = Audience.{aud}\n"
        )
        assert _ids(tmp_path / aud, src) == [], aud


def test_p041_silent_on_non_gate_broken_category(tmp_path: Path) -> None:
    """AUTH is genuinely the customer's to fix — USER is correct there."""
    src = _leaf(
        "    category: ClassVar[FailureCategory] = FailureCategory.AUTH\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    assert _ids(tmp_path, src) == []


def test_p041_silent_when_audience_only_inherited(tmp_path: Path) -> None:
    """Inheriting a corrected leaf is the right answer — do not flag it."""
    src = (
        "from application_sdk.errors.leaves import RateLimitedError\n"
        "\n\nclass SourceThrottled(RateLimitedError):\n    pass\n"
    )
    assert _ids(tmp_path, src) == []


def test_p041_silent_when_subclass_redeclares_a_safe_category(tmp_path: Path) -> None:
    """Redeclaring category leaves the gate-broken set; P002 owns that call."""
    src = (
        "from typing import ClassVar\n"
        "from application_sdk.errors.leaves import RateLimitedError\n"
        "from application_sdk.errors.wire import Audience, FailureCategory\n"
        "\n\nclass Rebranded(RateLimitedError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.AUTH\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    assert _ids(tmp_path, src) == []


def test_p041_suppressed(tmp_path: Path) -> None:
    src = _leaf(
        "    category: ClassVar[FailureCategory] = FailureCategory.RATE_LIMITED\n"
        "    # conformance: ignore[P041] intentional: customer-set quota, they own it\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    findings = _scan(tmp_path, {"m.py": src})
    assert [f.rule_id for f in findings] == ["P041"]
    assert findings[0].suppressed is True
