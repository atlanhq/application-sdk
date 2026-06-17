"""Meta-tests for the P-series prescription checks (P001, P002, P003).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations (BLDX-1394).  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false
negatives are guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.prescriptions import main, scan_all, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition, EnforcementTier


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, "x.py")]


def _scan_one(tmp_path: Path, src: str) -> list:
    """Convenience: write *src* to a single file and run the cross-file scanner."""
    p = tmp_path / "m.py"
    p.write_text(src)
    return scan_all([p], tmp_path)


def _scan_files(tmp_path: Path, files: dict[str, str]) -> list:
    """Write each *files* entry and run the cross-file scanner over all of them."""
    paths: list[Path] = []
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
        paths.append(path)
    return scan_all(paths, tmp_path)


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


# ── P003 ErrorCodePrefixMismatch ──────────────────────────────────────────────


def test_p003_silent_on_correctly_prefixed_subclass(tmp_path: Path) -> None:
    src = (
        "from typing import ClassVar\n"
        "class IamTokenError(AuthError):\n"
        '    code: ClassVar[str] = "AUTH_MYSQL_IAM_TOKEN"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == []


def test_p003_fires_on_wrong_prefix(tmp_path: Path) -> None:
    src = (
        "from typing import ClassVar\n"
        "class AppNotFoundError(InvalidInputError):\n"
        '    code: ClassVar[str] = "APP_NOT_FOUND"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert len(findings) == 1
    assert findings[0].rule_id == "P003"
    assert "INVALID_INPUT_" in findings[0].message
    assert "APP_NOT_FOUND" in findings[0].message


def test_p003_fires_on_missing_code_declaration(tmp_path: Path) -> None:
    """Subclasses (transitive) of a leaf must declare their own code."""
    src = "class SilentSubclass(InternalError):\n    pass\n"
    findings = _scan_one(tmp_path, src)
    assert len(findings) == 1
    assert findings[0].rule_id == "P003"
    assert "does not declare its own" in findings[0].message
    assert "INTERNAL_" in findings[0].message


def test_p003_fires_on_bare_leaf_prefix_without_underscore(tmp_path: Path) -> None:
    """A subclass that reuses the bare leaf code (e.g. ``"AUTH"``) collapses with
    the leaf — must add a suffix after the prefix underscore."""
    src = (
        "from typing import ClassVar\n"
        "class GenericAuth(AuthError):\n"
        '    code: ClassVar[str] = "AUTH"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_silent_on_leaf_class_itself(tmp_path: Path) -> None:
    """The 14 leaves themselves declare ``code = "<PREFIX>"`` — that's the
    canonical source, not a violation."""
    src = (
        "from typing import ClassVar\n"
        "class AuthError(AppError):\n"
        '    code: ClassVar[str] = "AUTH"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert findings == []


def test_p003_silent_on_classes_outside_apperror_tree(tmp_path: Path) -> None:
    """A class with a ``code`` attribute that isn't an AppError subclass is out of scope."""
    src = (
        "from typing import ClassVar\n"
        "class UnrelatedThing(SomeOtherBase):\n"
        '    code: ClassVar[str] = "WHATEVER"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert findings == []


def test_p003_resolves_transitive_inheritance_in_one_file(tmp_path: Path) -> None:
    """Pass-through intermediate (no ``code``) flagged; concrete grandchild OK."""
    src = (
        "from typing import ClassVar\n"
        "class _SqlAuth(AuthError):\n"  # intermediate, no code → flagged
        "    pass\n"
        "class SqlAuthFailed(_SqlAuth):\n"  # transitively under AuthError
        '    code: ClassVar[str] = "AUTH_SQL_FAILED"\n'
    )
    findings = _scan_one(tmp_path, src)
    rule_ids = [f.rule_id for f in findings]
    assert rule_ids == ["P003"]
    # The intermediate's missing-code message names the leaf prefix
    assert "AUTH" in findings[0].message
    assert "_SqlAuth" in findings[0].message


def test_p003_resolves_transitive_inheritance_across_files(tmp_path: Path) -> None:
    """The cross-file class registry walks bases that live in another module."""
    files = {
        "a.py": (
            "from typing import ClassVar\n"
            "class _SqlAuthBase(AuthError):\n"
            '    code: ClassVar[str] = "AUTH_SQL"\n'  # itself well-prefixed
        ),
        "b.py": (
            "from typing import ClassVar\n"
            "from a import _SqlAuthBase\n"
            "class WrongPrefixError(_SqlAuthBase):\n"
            '    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SQL_BAD"\n'
        ),
    }
    findings = _scan_files(tmp_path, files)
    rule_ids = [f.rule_id for f in findings]
    assert rule_ids == ["P003"]
    assert "AUTH_" in findings[0].message
    assert "WrongPrefixError" in findings[0].message


def test_p003_resolves_transitive_chain_with_intermediate_in_separate_file(
    tmp_path: Path,
) -> None:
    """B → A → InternalError, all in different files: B flagged on wrong prefix."""
    files = {
        "leaf_proxy.py": (
            "from typing import ClassVar\n"
            "class _IntermediateOk(InternalError):\n"
            '    code: ClassVar[str] = "INTERNAL_INTERMEDIATE"\n'
        ),
        "concrete.py": (
            "from typing import ClassVar\n"
            "class GrandchildBad(_IntermediateOk):\n"
            '    code: ClassVar[str] = "AUTH_MIXED_UP"\n'
        ),
    }
    findings = _scan_files(tmp_path, files)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_handles_attribute_access_in_base(tmp_path: Path) -> None:
    """``class Foo(application_sdk.errors.AuthError)`` resolves via attribute access."""
    src = (
        "import application_sdk.errors as errors\n"
        "from typing import ClassVar\n"
        "class WeirdAuth(errors.AuthError):\n"
        '    code: ClassVar[str] = "INTERNAL_WRONG"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_handles_aliased_leaf_import(tmp_path: Path) -> None:
    """A leaf imported under a private alias (``InternalError as _InternalError``)
    must still be recognised as the leaf — otherwise aliased subclasses become
    silent false-negatives."""
    src = (
        "from application_sdk.errors.leaves import InternalError as _InternalError\n"
        "from typing import ClassVar\n"
        "class AppContextError(_InternalError):\n"
        '    code: ClassVar[str] = "APP_CONTEXT"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]
    assert "INTERNAL_" in findings[0].message


def test_p003_aliased_import_with_correct_prefix_is_silent(tmp_path: Path) -> None:
    src = (
        "from application_sdk.errors.leaves import AuthError as _Auth\n"
        "from typing import ClassVar\n"
        "class TokenError(_Auth):\n"
        '    code: ClassVar[str] = "AUTH_OAUTH_TOKEN"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert findings == []


def test_p003_silent_when_no_classvar_annotation(tmp_path: Path) -> None:
    """Plain ``code = "..."`` (no ClassVar) still binds an attribute and is checked."""
    src = 'class WrongPlain(InternalError):\n    code = "AUTH_BAD"\n'
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_silent_on_abstract_intermediate_with_directive(tmp_path: Path) -> None:
    """An abstract intermediate without a ``code`` can opt out with a directive."""
    src = (
        "# conformance: ignore[P003] genuinely abstract intermediate\n"
        "class _AbstractMid(InternalError):\n"
        "    pass\n"
    )
    findings = _scan_one(tmp_path, src)
    # Finding still emitted, but suppressed=True → gate stays green
    assert [f.rule_id for f in findings] == ["P003"]
    assert findings[0].suppressed is True


def test_p003_suppressed_by_directive_on_code_line(tmp_path: Path) -> None:
    src = (
        "from typing import ClassVar\n"
        "class WrongCode(InvalidInputError):\n"
        '    code: ClassVar[str] = "APP_NOT_FOUND"  # conformance: ignore[P003] deprecated v4.0 shim\n'
    )
    findings = _scan_one(tmp_path, src)
    assert findings[0].suppressed is True
    assert "shim" in (findings[0].suppression_justification or "")


def test_p003_handles_cycle_without_recursion_error(tmp_path: Path) -> None:
    """Self-referential or mutually-recursive base chains must not raise."""
    files = {
        "a.py": "class A(B): pass\n",
        "b.py": "class B(A): pass\n",
    }
    # Should resolve to None for both (cycle, no leaf reached) and emit nothing.
    findings = _scan_files(tmp_path, files)
    assert findings == []


def test_p003_first_wins_on_global_class_name_collision(tmp_path: Path) -> None:
    """When two files define a class with the same name, the first-seen record
    is used as the resolution source — this is best-effort, not a feature."""
    # Test purely confirms no crash; behaviour documented in scan_all.
    files = {
        "a.py": "class _Pivot(AuthError):\n    pass\n",
        "b.py": "class _Pivot(InternalError):\n    pass\n",
    }
    findings = _scan_files(tmp_path, files)
    # Both _Pivot definitions are intermediates without code — both flagged.
    assert all(f.rule_id == "P003" for f in findings)
    assert len(findings) == 2


def test_p003_collision_concrete_child_resolves_to_first_seen_prefix(
    tmp_path: Path,
) -> None:
    """A downstream child of the colliding name resolves to the first-seen definition."""
    files = {
        "a.py": "class _Pivot(AuthError):\n    pass\n",  # first-seen → AUTH
        "b.py": "class _Pivot(InternalError):\n    pass\n",
        "c.py": (
            "from typing import ClassVar\n"
            "class Concrete(_Pivot):\n"
            '    code: ClassVar[str] = "INTERNAL_CONCRETE"\n'
        ),
    }
    findings = _scan_files(tmp_path, files)
    concrete = [f for f in findings if "Concrete" in f.message]
    assert len(concrete) == 1
    # First-seen _Pivot is from a.py (AuthError) — INTERNAL_ prefix is wrong
    assert "AUTH_" in concrete[0].message


def test_p003_first_base_wins_on_multiple_leaf_bases(tmp_path: Path) -> None:
    """class X(AuthError, InternalError): first base in list order picks the prefix."""
    src = (
        "from typing import ClassVar\n"
        "class MultiLeaf(AuthError, InternalError):\n"
        '    code: ClassVar[str] = "INTERNAL_MIXED"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]
    # AUTH wins (first base) — INTERNAL_ code is wrong
    assert "AUTH_" in findings[0].message


def test_p003_multi_leaf_correct_first_prefix_is_silent(tmp_path: Path) -> None:
    src = (
        "from typing import ClassVar\n"
        "class MultiLeaf(AuthError, InternalError):\n"
        '    code: ClassVar[str] = "AUTH_MIXED"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert findings == []


def test_p003_fires_on_empty_code(tmp_path: Path) -> None:
    src = (
        "from typing import ClassVar\n"
        "class EmptyCode(AuthError):\n"
        '    code: ClassVar[str] = ""\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_fires_on_lowercase_prefix(tmp_path: Path) -> None:
    """Prefix match is case-sensitive (startswith); lowercase prefix must fire."""
    src = (
        "from typing import ClassVar\n"
        "class LowerPrefix(AuthError):\n"
        '    code: ClassVar[str] = "auth_token"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_recognises_leaf_via_star_import(tmp_path: Path) -> None:
    """from … import * then class Foo(InternalError): leaf resolved via LEAF_PREFIX_MAP."""
    src = (
        "from application_sdk.errors.leaves import *\n"
        "from typing import ClassVar\n"
        "class StarImportError(InternalError):\n"
        '    code: ClassVar[str] = "AUTH_WRONG"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]
    assert "INTERNAL_" in findings[0].message


def test_p003_recognises_leaf_via_init_reexport(tmp_path: Path) -> None:
    """Leaf re-exported through a package __init__ is still recognised."""
    files = {
        "myerrors/__init__.py": "from application_sdk.errors.leaves import AuthError\n",
        "consumer.py": (
            "from myerrors import AuthError\n"
            "from typing import ClassVar\n"
            "class BadCode(AuthError):\n"
            '    code: ClassVar[str] = "INTERNAL_BAD"\n'
        ),
    }
    findings = _scan_files(tmp_path, files)
    assert [f.rule_id for f in findings] == ["P003"]
    assert "AUTH_" in findings[0].message


# ── P003 tier / disposition / gate ─────────────────────────────────────────────


def test_p003_is_block_tier() -> None:
    assert get_rule("P003").tier is EnforcementTier.BLOCK


def test_p003_block_violation_fails_the_gate(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class WrongCode(AuthError):\n"
        '    code: ClassVar[str] = "INTERNAL_SOMETHING"\n'
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p003_suppressed_declaration_keeps_gate_green(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class WrongCode(AuthError):\n"
        '    code: ClassVar[str] = "INTERNAL_X"  # conformance: ignore[P003] deprecated v4.0 shim\n'
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_p003_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class WrongCode(AuthError):\n"
        '    code: ClassVar[str] = "INTERNAL_X"\n'
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
    assert Disposition.FAILING in dispositions


def test_p003_sarif_output_validates(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class WrongCode(AuthError):\n"
        '    code: ClassVar[str] = "INTERNAL_X"\n'
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
    src = "class DomainErr(InternalError):\n    category = FailureCategory.INTERNAL\n"
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
    src = "class Product(BaseModel):\n    category: str = 'electronics'\n"
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


def test_p002_fires_on_subscripted_leaf_base() -> None:
    # ``class Foo(NotFoundError[T])`` — base is Subscript; _get_name must
    # unwrap it to reach the Name, otherwise the class escapes detection.
    src = (
        "from typing import ClassVar, TypeVar\n"
        "T = TypeVar('T')\n"
        "class DomainErr(NotFoundError[T]):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_fires_on_subscripted_attribute_base() -> None:
    # ``class Foo(errors.NotFoundError[T])`` — Subscript wrapping an Attribute.
    src = (
        "from typing import ClassVar, TypeVar\n"
        "T = TypeVar('T')\n"
        "class DomainErr(errors.NotFoundError[T]):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    assert _ids(src) == ["P002"]


def test_p002_fires_on_second_generation_override() -> None:
    # ``class TenantErr(SourceErr)`` where ``SourceErr`` is itself a
    # first-generation override.  Transitive closure must catch it.
    src = (
        "from typing import ClassVar\n"
        "class SourceErr(AppTimeoutError):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.TIMEOUT\n"
        "class TenantErr(SourceErr):\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.TIMEOUT\n"
    )
    findings = scan_text(src, "x.py")
    rule_ids = [f.rule_id for f in findings]
    assert rule_ids.count("P002") == 2


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
        '    code: ClassVar[str] = "NOT_FOUND_DOMAIN"\n'
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p002_suppressed_declaration_keeps_gate_green(tmp_path: Path) -> None:
    """A justified inline suppression clears the gate (exit 0) — suppress-only path."""
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        '    code: ClassVar[str] = "NOT_FOUND_DOMAIN"\n'
        "    # conformance: ignore[P002] redundant redeclaration\n"
        "    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_p002_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from typing import ClassVar\n"
        "class DomainErr(NotFoundError):\n"
        '    code: ClassVar[str] = "NOT_FOUND_DOMAIN"\n'
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
        '    code: ClassVar[str] = "NOT_FOUND_DOMAIN"\n'
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
