"""Meta-tests for B005 NonAdditiveContractChange and B006 StaleContractLedger.

Both rules are BLOCK from day 0.  These tests guard against:
- false negatives (a violation that should fire, but doesn't), and
- false positives (a safe change that incorrectly fires).

Test helpers
------------
``_scan`` writes a set of Python source files to a tmp directory, populates a
ledger with any provided entries, and calls ``scan_contract_compat`` directly.
``_scan_via_suite`` calls the full B-series ``scan_all`` to confirm the rules
integrate end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.deprecation._contract_compat import scan_contract_compat
from conformance.suite.checks.deprecation._ledger_schema import (
    ContractField,
    ContractLedger,
    serialize,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_ledger(*fields: ContractField) -> ContractLedger:
    return ContractLedger(version=1, fields=list(fields))


def _scan(
    tmp_path: Path,
    files: dict[str, str],
    ledger: ContractLedger | None = None,
) -> list:
    """Write *files* to *tmp_path* and run scan_contract_compat."""
    paths: list[Path] = []
    for name, src in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    if ledger is None:
        ledger = ContractLedger(version=1, fields=[])
    return scan_contract_compat(paths, tmp_path, ledger)


def _ids(findings: list) -> list[str]:
    """Return rule IDs of unsuppressed findings (matching runner gate semantics)."""
    return [f.rule_id for f in findings if not f.suppressed]


# ── Rule metadata ─────────────────────────────────────────────────────────────


def test_b005_is_block_tier() -> None:
    rule = get_rule("B005")
    assert rule.tier is EnforcementTier.BLOCK


def test_b006_is_block_tier() -> None:
    rule = get_rule("B006")
    assert rule.tier is EnforcementTier.BLOCK


# ── B005: field removed ───────────────────────────────────────────────────────

_EP_WITH_RUN = """\
from application_sdk.app import App

class MyInput:
    name: str

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_EP_WITHOUT_FIELD = """\
from application_sdk.app import App

class MyInput:
    pass  # 'name' field removed

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""


def test_b005_field_removed_fires(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_WITHOUT_FIELD}, ledger)
    assert "B005" in _ids(findings)


def test_b005_field_still_present_silent(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_WITH_RUN}, ledger)
    assert "B005" not in _ids(findings)


# ── B005: type changed ────────────────────────────────────────────────────────

_EP_TYPE_INT = """\
from application_sdk.app import App

class MyInput:
    count: int

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""


def test_b005_type_changed_fires(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("MyInput", "count", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_TYPE_INT}, ledger)
    b005 = [f for f in findings if f.rule_id == "B005"]
    assert b005, "expected B005 on type change"
    assert "type changed" in b005[0].message.lower()


def test_b005_same_type_silent(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("MyInput", "count", "int", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_TYPE_INT}, ledger)
    assert "B005" not in _ids(findings)


# ── B005: Optional normalization prevents false positives ─────────────────────

_EP_OPTIONAL = """\
from application_sdk.app import App
from typing import Optional

class MyInput:
    name: Optional[str]

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_EP_UNION_NONE = """\
from application_sdk.app import App

class MyInput:
    name: str | None

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""


def test_b005_optional_and_union_none_equivalent(tmp_path: Path) -> None:
    """Optional[str] and str | None normalize to the same canonical type."""
    # Ledger recorded as Optional[str] normalizes to str | None
    ledger = _make_ledger(ContractField("MyInput", "name", "str | None", "active"))
    findings_opt = _scan(tmp_path / "opt", {"app.py": _EP_OPTIONAL}, ledger)
    findings_union = _scan(tmp_path / "union", {"app.py": _EP_UNION_NONE}, ledger)
    assert "B005" not in _ids(findings_opt), "Optional[str] should match str | None"
    assert "B005" not in _ids(findings_union), "str | None should match str | None"


# ── B005: canonical type normalization (typing aliases + Annotated + Union) ───

_EP_LIST_STR = """\
from application_sdk.app import App
from typing import List

class MyInput:
    items: List[str]

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_EP_LIST_STR_LOWER = """\
from application_sdk.app import App

class MyInput:
    items: list[str]

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_EP_DICT = """\
from application_sdk.app import App
from typing import Dict

class MyInput:
    mapping: Dict[str, int]

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_EP_ANNOTATED = """\
from application_sdk.app import App
from typing import Annotated

class MyInput:
    count: Annotated[int, "metadata"]

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_EP_UNION_THREE = """\
from application_sdk.app import App
from typing import Union

class MyInput:
    value: Union[str, int, None]

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""


def test_b005_list_capitalized_equivalent(tmp_path: Path) -> None:
    """List[str] and list[str] normalize to the same canonical type."""
    ledger = _make_ledger(ContractField("MyInput", "items", "list[str]", "active"))
    findings_cap = _scan(tmp_path / "cap", {"app.py": _EP_LIST_STR}, ledger)
    findings_low = _scan(tmp_path / "low", {"app.py": _EP_LIST_STR_LOWER}, ledger)
    assert "B005" not in _ids(findings_cap), "List[str] should match list[str]"
    assert "B005" not in _ids(findings_low), "list[str] should match list[str]"


def test_b005_dict_capitalized_equivalent(tmp_path: Path) -> None:
    """Dict[str, int] and dict[str, int] normalize to the same canonical type."""
    ledger = _make_ledger(
        ContractField("MyInput", "mapping", "dict[str, int]", "active")
    )
    findings = _scan(tmp_path, {"app.py": _EP_DICT}, ledger)
    assert "B005" not in _ids(findings), "Dict[str, int] should match dict[str, int]"


def test_b005_annotated_strips_metadata(tmp_path: Path) -> None:
    """Annotated[int, ...] strips metadata and matches plain int."""
    ledger = _make_ledger(ContractField("MyInput", "count", "int", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_ANNOTATED}, ledger)
    assert "B005" not in _ids(findings), "Annotated[int, ...] should match int"


def test_b005_union_three_way(tmp_path: Path) -> None:
    """Union[str, int, None] normalizes to str | int | None."""
    ledger = _make_ledger(
        ContractField("MyInput", "value", "str | int | None", "active")
    )
    findings = _scan(tmp_path, {"app.py": _EP_UNION_THREE}, ledger)
    assert "B005" not in _ids(
        findings
    ), "Union[str, int, None] should match str | int | None"


# ── B005: @entrypoint decorator variant ───────────────────────────────────────

_EP_DECORATOR = """\
from application_sdk.app import App, entrypoint

class ExtractInput:
    url: str

class ExtractOutput:
    count: int

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        pass
"""

_EP_DECORATOR_FIELD_REMOVED = """\
from application_sdk.app import App, entrypoint

class ExtractInput:
    pass  # url removed

class ExtractOutput:
    count: int

class MyApp(App):
    @entrypoint
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        pass
"""


def test_b005_entrypoint_decorator_input_fires(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("ExtractInput", "url", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_DECORATOR_FIELD_REMOVED}, ledger)
    assert "B005" in _ids(findings)


def test_b005_entrypoint_decorator_output_fires(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("ExtractOutput", "count", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_DECORATOR}, ledger)
    b005 = [f for f in findings if f.rule_id == "B005"]
    assert b005, "expected B005 on Output type change"


# ── B005: @task contracts are excluded ───────────────────────────────────────

_TASK_CONTRACT = """\
from application_sdk.app import App, task

class TaskInput:
    data: str

class MyApp(App):
    @task
    async def process(self, input: TaskInput) -> None:
        pass
"""


def test_task_contract_excluded(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("TaskInput", "data", "str", "active"))
    # Remove the field from source — but it's a @task contract, so B005 must NOT fire
    src = _TASK_CONTRACT.replace("    data: str\n", "")
    findings = _scan(tmp_path, {"app.py": src}, ledger)
    assert "B005" not in _ids(
        findings
    ), "@task contract changes must never trigger B005"


# ── B006: new field not in ledger ─────────────────────────────────────────────


def test_b006_new_field_not_in_ledger_fires(tmp_path: Path) -> None:
    # Ledger is empty but contract has a field → B006
    findings = _scan(tmp_path, {"app.py": _EP_WITH_RUN})
    assert "B006" in _ids(findings)


def test_b006_field_recorded_silent(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_WITH_RUN}, ledger)
    assert "B006" not in _ids(findings)
    assert "B005" not in _ids(findings)


# ── B005 suppress via inline directive ────────────────────────────────────────

_EP_SUPPRESSED = """\
from application_sdk.app import App

# conformance: ignore[B005] no deployed consumers yet
class MyInput:
    pass  # field removed — directive on line above this class

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""


def test_b005_inline_suppress_clears(tmp_path: Path) -> None:
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(tmp_path, {"app.py": _EP_SUPPRESSED}, ledger)
    assert "B005" not in _ids(findings)


# ── Inheritance: in-repo base class (cross-file) ──────────────────────────────

_BASE_FILE = """\
class BaseInput:
    name: str
"""

_SUBCLASS_FILE = """\
from application_sdk.app import App
from base import BaseInput

class MyInput(BaseInput):
    pass

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""

_SUBCLASS_FILE_OVERRIDE = """\
from application_sdk.app import App
from base import BaseInput

class MyInput(BaseInput):
    name: int  # own-body override changes the type

class MyApp(App):
    async def run(self, input: MyInput) -> None:
        pass
"""


def test_b005_inherited_field_across_files_not_flagged_removed(
    tmp_path: Path,
) -> None:
    """A field declared only on a cross-file base class is not 'removed'."""
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(
        tmp_path,
        {"base.py": _BASE_FILE, "app.py": _SUBCLASS_FILE},
        ledger,
    )
    assert "B005" not in _ids(findings)


def test_b006_inherited_field_across_files_tracked_silent(tmp_path: Path) -> None:
    """A ledger-recorded field inherited from a cross-file base is not 'stale'."""
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(
        tmp_path,
        {"base.py": _BASE_FILE, "app.py": _SUBCLASS_FILE},
        ledger,
    )
    assert "B006" not in _ids(findings)


def test_b006_inherited_field_not_yet_in_ledger_fires(tmp_path: Path) -> None:
    """An inherited field with no ledger entry at all is still B006-visible."""
    findings = _scan(tmp_path, {"base.py": _BASE_FILE, "app.py": _SUBCLASS_FILE})
    b006 = [f for f in findings if f.rule_id == "B006"]
    assert any("MyInput.name" in f.message for f in b006)
    assert any("inherited" in f.message for f in b006)


def test_b005_own_body_override_wins_over_inherited(tmp_path: Path) -> None:
    """A subclass redeclaring an inherited field changes its live type."""
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(
        tmp_path,
        {"base.py": _BASE_FILE, "app.py": _SUBCLASS_FILE_OVERRIDE},
        ledger,
    )
    b005 = [f for f in findings if f.rule_id == "B005"]
    assert b005, "own-body override to a different type must still fire B005"
    assert "type changed" in b005[0].message.lower()


# ── Inheritance: ClassVar on a base class is excluded ─────────────────────────

_BASE_WITH_CLASSVAR = """\
from typing import ClassVar

class BaseInput:
    TEMPLATE: ClassVar[str] = "x"
    name: str
"""


def test_classvar_on_base_class_excluded_from_resolution(tmp_path: Path) -> None:
    """A ClassVar sentinel on a base class is never treated as a contract field."""
    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    findings = _scan(
        tmp_path,
        {"base.py": _BASE_WITH_CLASSVAR, "app.py": _SUBCLASS_FILE},
        ledger,
    )
    # If TEMPLATE were (incorrectly) resolved as a field, it would be untracked
    # and fire B006. Only "name" is live and it is ledger-recorded, so no B005/B006.
    assert "B006" not in _ids(findings)
    assert "B005" not in _ids(findings)


# ── Inheritance: SDK-provided mixin resolved via static registry ─────────────

_EP_MIXIN_OUTPUT = """\
from application_sdk.app import App, entrypoint
from application_sdk.contracts.base import PublishInputMixin

class MyInput:
    url: str

class MyOutput(PublishInputMixin):
    custom: str

class MyApp(App):
    @entrypoint
    async def extract(self, input: MyInput) -> MyOutput:
        pass
"""


def test_b005_sdk_mixin_field_resolved_via_static_registry(tmp_path: Path) -> None:
    """A field from an SDK mixin with no in-repo definition resolves via the registry."""
    ledger = _make_ledger(
        ContractField("MyOutput", "connection_qualified_name", "str", "active")
    )
    findings = _scan(tmp_path, {"app.py": _EP_MIXIN_OUTPUT}, ledger)
    assert "B005" not in _ids(
        findings
    ), "PublishInputMixin.connection_qualified_name must resolve via the SDK registry"


def test_b006_sdk_mixin_field_not_yet_in_ledger_fires_and_notes_inherited(
    tmp_path: Path,
) -> None:
    """An untracked SDK-mixin field still fires B006, anchored on the subclass."""
    findings = _scan(tmp_path, {"app.py": _EP_MIXIN_OUTPUT})
    b006 = [f for f in findings if f.rule_id == "B006"]
    mixin_findings = [
        f for f in b006 if "MyOutput.transformed_data_prefix" in f.message
    ]
    assert mixin_findings, "expected B006 for the untracked mixin field"
    assert "inherited" in mixin_findings[0].message
    assert mixin_findings[0].file == "app.py"


# ── End-to-end via full B-series scan_all ─────────────────────────────────────


def test_b005_block_violation_fails_gate(tmp_path: Path) -> None:
    """B005 fires on a removed field, and the runner exits 1 (BLOCK tier)."""
    import os
    import subprocess
    import sys

    src_file = tmp_path / "app.py"
    src_file.write_text(_EP_WITHOUT_FIELD, encoding="utf-8")

    ledger = _make_ledger(ContractField("MyInput", "name", "str", "active"))
    ledger_file = tmp_path / "contract_schema.lock.json"
    ledger_file.write_text(serialize(ledger), encoding="utf-8")

    # Direct scan: B005 must fire
    findings = scan_contract_compat([src_file], tmp_path, ledger)
    assert "B005" in {f.rule_id for f in findings if not f.suppressed}

    # Subprocess: runner must exit 1; pass the temp ledger via env var.
    # Add conformance package to PYTHONPATH so the subprocess can import it
    # regardless of which venv Python is resolved to by sys.executable.
    conformance_pkg_root = str(Path(__file__).parent.parent)
    pythonpath = conformance_pkg_root + os.pathsep + os.environ.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "conformance.suite.runner",
            "--repo",
            str(tmp_path),
            "--series",
            "B",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ATLAN_CONTRACT_LEDGER_PATH": str(ledger_file),
            "PYTHONPATH": pythonpath,
        },
    )
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "B005" in result.stdout + result.stderr
