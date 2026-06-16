"""Meta-tests for the P-series prescription checks (P001, P002).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations (BLDX-1394).  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false
negatives are guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.prescriptions import main, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition, EnforcementTier


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, "x.py")]


# ── P001 UnboundedContractFields ───────────────────────────────────────────────


def test_p001_fires_on_unbounded_input() -> None:
    src = "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    findings = scan_text(src, "x.py")
    assert [f.rule_id for f in findings] == ["P001"]
    assert findings[0].line == 1


def test_p001_fires_with_mixin_and_other_bases() -> None:
    src = "class O(PublishInputMixin, Output, allow_unbounded_fields=True):\n    x: str = ''\n"
    assert _ids(src) == ["P001"]


def test_p001_silent_on_plain_contract() -> None:
    assert _ids("class MyInput(Input):\n    pass\n") == []


def test_p001_silent_when_explicitly_false() -> None:
    src = "class MyInput(Input, allow_unbounded_fields=False):\n    pass\n"
    assert _ids(src) == []


def test_p001_silent_on_falsy_literals() -> None:
    # Literal-falsy values are genuine opt-back-ins (runtime `if x:` is False).
    for val in ("None", "0", "''"):
        src = f"class MyInput(Input, allow_unbounded_fields={val}):\n    pass\n"
        assert _ids(src) == [], f"should not fire on ={val}"


def test_p001_fires_on_truthy_non_true_values() -> None:
    # Runtime opt-out is `if allow_unbounded_fields:` — any truthy value opts
    # out, so =1 must be caught (was a false-negative when matching only `True`).
    assert _ids("class A(Input, allow_unbounded_fields=1):\n    pass\n") == ["P001"]


def test_p001_fires_on_dynamic_value() -> None:
    # A runtime-controlled opt-out on a BLOCK rule must be surfaced for review,
    # not silently missed.
    assert _ids(
        "FLAG = True\nclass A(Input, allow_unbounded_fields=FLAG):\n    pass\n"
    ) == ["P001"]
    assert _ids("class A(Input, allow_unbounded_fields=(1 == 1)):\n    pass\n") == [
        "P001"
    ]


def test_p001_silent_on_unrelated_class_keyword() -> None:
    # A different class keyword (e.g. metaclass) must not trip the rule.
    src = "class MyInput(Input, metaclass=ABCMeta):\n    pass\n"
    assert _ids(src) == []


def test_p001_suppressed_by_directive_line_above() -> None:
    src = (
        "# conformance: ignore[P001] generic cleanup payload — fields vary by app\n"
        "class CleanupInput(Input, allow_unbounded_fields=True):\n"
        "    pass\n"
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].rule_id == "P001"
    assert findings[0].suppressed is True
    assert "cleanup" in (findings[0].suppression_justification or "")


def test_p001_suppressed_by_trailing_directive() -> None:
    src = "class CleanupInput(Input, allow_unbounded_fields=True):  # conformance: ignore[P001] legit\n    pass\n"
    findings = scan_text(src, "x.py")
    assert findings[0].suppressed is True


# ── tier / disposition / gate ───────────────────────────────────────────────────


def test_p001_is_block_tier() -> None:
    # P-series is suppress-only / above-the-bar (BLDX-1428).
    assert get_rule("P001").tier is EnforcementTier.BLOCK


def test_p001_block_violation_fails_the_gate(tmp_path: Path) -> None:
    """An unsuppressed P001 (BLOCK) declaration fails the gate (exit 1)."""
    (tmp_path / "m.py").write_text(
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p001_suppressed_declaration_keeps_gate_green(tmp_path: Path) -> None:
    """A justified inline suppression clears the gate (exit 0) — suppress-only path."""
    (tmp_path / "m.py").write_text(
        "# conformance: ignore[P001] generic cleanup payload\n"
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_p001_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    dispositions = [derive_disposition(r) for r in report.runs[0].results]
    assert dispositions == [Disposition.FAILING]


def test_p001_sarif_output_validates(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    validate_sarif(report)


# ── P002 CategoryFieldOverride ─────────────────────────────────────────────────


def test_p002_fires_on_leaf_subclass_redeclaring_category() -> None:
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    code: ClassVar[str] = 'X'\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    findings = scan_text(src, "x.py")
    assert [f.rule_id for f in findings] == ["P002"]
    # Finding should anchor on the assignment line, not the class header.
    assert findings[0].line == 4


def test_p002_fires_on_plain_assignment_no_annotation() -> None:
    src = (
        "class DomainErr(InternalError):\n"
        "    category = FailureCategory.INTERNAL\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_fires_on_divergent_value_override() -> None:
    # Worst case: a true taxonomy override (subclass picks a different category).
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(InternalError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.INVALID_INPUT\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_fires_on_attribute_base() -> None:
    # ``class Foo(errors.NotFoundError)`` — base is Attribute; matched on simple name.
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(errors.NotFoundError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_fires_when_leaf_is_second_base() -> None:
    # Real SDK pattern: ``class StorageNotFoundError(NotFoundError, StorageError)``.
    # Categorical leaf may appear at any position in the bases.
    src = (
        "from typing import ClassVar\n"
        "class StorageNotFoundError(NotFoundError, StorageError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_fires_on_appbase_subclass_redeclaring_category() -> None:
    # A class that subclasses ``AppError`` directly (creating a "new leaf") and
    # assigns ``category`` is still a taxonomy expansion — flag it.
    src = (
        "from typing import ClassVar\n"
        "class CustomLeaf(AppError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.INTERNAL\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_silent_on_canonical_leaves_themselves() -> None:
    # The 14 canonical leaves are the *defining sites* — they MUST set category.
    for leaf, cat in (
        ("CancelledError", "CANCELLED"),
        ("AppTimeoutError", "TIMEOUT"),
        ("RateLimitedError", "RATE_LIMITED"),
        ("AuthError", "AUTH"),
        ("AppPermissionDeniedError", "PERMISSION"),
        ("NotFoundError", "NOT_FOUND"),
        ("AlreadyExistsError", "ALREADY_EXISTS"),
        ("InvalidInputError", "INVALID_INPUT"),
        ("PreconditionError", "PRECONDITION"),
        ("DependencyUnavailableError", "DEPENDENCY_UNAVAILABLE"),
        ("ResourceExhaustedError", "RESOURCE_EXHAUSTED"),
        ("DataIntegrityError", "DATA_INTEGRITY"),
        ("InternalError", "INTERNAL"),
        ("UnimplementedError", "UNIMPLEMENTED"),
    ):
        src = (
            f"from typing import ClassVar\n"
            f"class {leaf}(AppError):\n"
            f"    category: ClassVar[FailureCategory] = FailureCategory.{cat}\n"
        )
        assert _ids(src) == [], f"should not fire on canonical leaf {leaf}"


def test_p002_silent_on_apperror_base_class_itself() -> None:
    # ``AppError`` itself defines the default category; not a redeclaration.
    src = (
        "from typing import ClassVar\n"
        "class AppError(Exception):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.INTERNAL\n"
    )
    assert _ids(src) == []


def test_p002_silent_on_subclass_that_does_not_redeclare_category() -> None:
    # The desired pattern: domain subclass overrides ``code``, inherits category.
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    code: ClassVar[str] = 'DOMAIN_NOT_FOUND'\n"
        "    resource_type: str | None = 'widget'\n"
    )
    assert _ids(src) == []


def test_p002_silent_on_unrelated_class_assigning_category() -> None:
    # A class that doesn't inherit from any AppError leaf may use a field named
    # ``category`` for its own purposes — must not fire.
    src = (
        "class Product(BaseModel):\n"
        "    category: str = 'electronics'\n"
    )
    assert _ids(src) == []


def test_p002_silent_on_annotation_only_no_value() -> None:
    # ``category: ClassVar[FailureCategory]`` with no ``= ...`` is a type-only
    # refinement, not a value redeclaration — does not drift the taxonomy.
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    category: ClassVar[FailureCategory]\n"
    )
    assert _ids(src) == []


def test_p002_silent_when_category_assigned_inside_method() -> None:
    # Instance-level mutation inside a method body is a different antipattern
    # and not what P002 is scoped to detect — only class-body redeclarations.
    src = (
        "class DomainErr(NotFoundError):\n"
        "    def reset(self) -> None:\n"
        "        category = FailureCategory.NOT_FOUND\n"
        "        self.x = category\n"
    )
    assert _ids(src) == []


def test_p002_suppressed_by_directive_line_above() -> None:
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    code: ClassVar[str] = 'X'\n"
        "    # conformance: ignore[P002] redundant — tracked for cleanup\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].rule_id == "P002"
    assert findings[0].suppressed is True
    assert "redundant" in (findings[0].suppression_justification or "")


def test_p002_suppressed_by_trailing_directive() -> None:
    src = (
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND  # conformance: ignore[P002] legit\n"
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_p002_does_not_match_p001_directive() -> None:
    # A P001 suppression must not absorb a P002 finding.
    src = (
        "from typing import ClassVar\n"
        "# conformance: ignore[P001] something else\n"
        "class DomainErr(NotFoundError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].rule_id == "P002"
    assert findings[0].suppressed is False


# ── P002 tier / disposition / gate ─────────────────────────────────────────────


def test_p002_is_block_tier() -> None:
    # P-series is suppress-only / above-the-bar (BLDX-1432).
    assert get_rule("P002").tier is EnforcementTier.BLOCK


def test_p002_block_violation_fails_the_gate(tmp_path: Path) -> None:
    """An unsuppressed P002 (BLOCK) declaration fails the gate (exit 1)."""
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p002_suppressed_declaration_keeps_gate_green(tmp_path: Path) -> None:
    """A justified inline suppression clears the gate (exit 0) — suppress-only path."""
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    # conformance: ignore[P002] redundant redeclaration\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_p002_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    dispositions = [derive_disposition(r) for r in report.runs[0].results]
    assert dispositions == [Disposition.FAILING]


def test_p002_sarif_output_validates(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    validate_sarif(report)
