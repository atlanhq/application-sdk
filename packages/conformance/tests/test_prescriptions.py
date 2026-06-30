"""Meta-tests for the P-series prescription checks (P001–P015).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations (BLDX-1394).  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false
negatives are guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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


def test_p003_fires_on_plain_code_assign_without_classvar_annotation(
    tmp_path: Path,
) -> None:
    """Plain ``code = "..."`` (no ClassVar annotation) is still extracted and checked."""
    src = 'class WrongPlain(InternalError):\n    code = "AUTH_BAD"\n'
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_silent_on_non_string_code_constant(tmp_path: Path) -> None:
    """``code = 123`` (non-string Constant) is ignored — not extracted as a code string."""
    src = "class NumericCode(InternalError):\n    code = 123\n"
    findings = _scan_one(tmp_path, src)
    # Treated as missing code declaration; still fires P003 but must not crash.
    assert [f.rule_id for f in findings] == ["P003"]
    assert "does not declare" in findings[0].message


def test_p003_silent_on_dynamic_code_value(tmp_path: Path) -> None:
    """``code = SOME_VAR`` (non-Constant) is not extracted — must not crash."""
    src = "CODE = 'INTERNAL_X'\nclass DynCode(InternalError):\n    code = CODE\n"
    findings = _scan_one(tmp_path, src)
    # Treated as missing code declaration; still fires P003 but must not crash.
    assert [f.rule_id for f in findings] == ["P003"]
    assert "does not declare" in findings[0].message


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


def test_p003_detects_nested_class(tmp_path: Path) -> None:
    """ast.walk descends into nested ClassDef bodies."""
    src = (
        "from typing import ClassVar\n"
        "class Outer:\n"
        "    class Inner(InternalError):\n"
        '        code: ClassVar[str] = "AUTH_WRONG"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_detects_decorated_class(tmp_path: Path) -> None:
    """@decorator does not prevent ClassDef from being found."""
    src = (
        "from dataclasses import dataclass\n"
        "from typing import ClassVar\n"
        "@dataclass\n"
        "class DataError(InternalError):\n"
        '    code: ClassVar[str] = "AUTH_WRONG"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_handles_subscript_leaf_base(tmp_path: Path) -> None:
    """class X(InternalError, Generic[T]) — Subscript base handled by _get_name."""
    src = (
        "from typing import ClassVar, Generic, TypeVar\n"
        "T = TypeVar('T')\n"
        "class GenericError(InternalError, Generic[T]):\n"
        '    code: ClassVar[str] = "AUTH_WRONG"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


def test_p003_detects_class_inside_type_checking_block(tmp_path: Path) -> None:
    """ClassDef inside TYPE_CHECKING is found by ast.walk."""
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    class FakeError(InternalError):\n"
        "        pass\n"
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "collect_import_aliases uses iter_child_nodes (top-level only) so "
        "aliases inside 'if TYPE_CHECKING:' are invisible — known false-negative"
    ),
)
def test_p003_type_checking_aliased_import_is_false_negative(tmp_path: Path) -> None:
    """Leaf alias imported inside TYPE_CHECKING block escapes alias resolution."""
    src = (
        "from __future__ import annotations\n"
        "if TYPE_CHECKING:\n"
        "    from application_sdk.errors.leaves import InternalError as _IE\n"
        "from typing import ClassVar\n"
        "class Foo(_IE):\n"
        '    code: ClassVar[str] = "AUTH_WRONG"\n'
    )
    findings = _scan_one(tmp_path, src)
    assert [f.rule_id for f in findings] == ["P003"]


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


# ── P013 UntypedEntrypointBoundary ────────────────────────────────────────────

# Shared contract fixtures for P013/P014 tests
_TYPED_CONTRACTS = (
    "from application_sdk.contracts import Input, Output\n"
    "\n"
    "class FetchInput(Input):\n"
    "    connection_id: str = ''\n"
    "\n"
    "class FetchOutput(Output):\n"
    "    rows: int = 0\n"
)

_APP_IMPORTS = "from application_sdk.app import App, entrypoint, task, signal, query\n"


def test_p013_fires_on_untyped_dict_input(tmp_path: Path) -> None:
    """@entrypoint with dict input → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


def test_p013_fires_on_subscripted_dict_input(tmp_path: Path) -> None:
    """@entrypoint with dict[str,str] input — 'even if bounded' → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: dict[str, str]) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1


def test_p013_fires_on_any_input(tmp_path: Path) -> None:
    """@entrypoint with Any input → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Any\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: Any) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1


def test_p013_fires_on_missing_input_annotation(tmp_path: Path) -> None:
    """@entrypoint with unannotated input param → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "missing" in p013[0].message.lower()


def test_p013_fires_on_untyped_return(tmp_path: Path) -> None:
    """@entrypoint with dict return type → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput) -> dict:\n"
            "        return {}\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "output" in p013[0].message


def test_p013_fires_on_missing_return_annotation(tmp_path: Path) -> None:
    """@entrypoint with no return annotation → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput):\n"
            "        pass\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "missing" in p013[0].message.lower()


def test_p013_fires_on_plain_base_model_boundary(tmp_path: Path) -> None:
    """@entrypoint whose input is a plain BaseModel (not Input subclass) → P013."""
    files = {
        "contracts.py": (
            "from pydantic import BaseModel\n"
            "\n"
            "class FetchInput(BaseModel):\n"
            "    connection_id: str = ''\n"
        ),
        "out.py": (
            "from application_sdk.contracts import Output\n"
            "class FetchOutput(Output):\n"
            "    rows: int = 0\n"
        ),
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "from out import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "FetchInput" in p013[0].message


def test_p013_silent_on_correctly_typed_entrypoint(tmp_path: Path) -> None:
    """@entrypoint with proper Input/Output subclasses → no P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput, FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert p013 == []


def test_p013_silent_on_contracts_in_same_file(tmp_path: Path) -> None:
    """Contracts defined in the same file as the App → resolved correctly."""
    src = (
        "from application_sdk.contracts import Input, Output\n"
        "from application_sdk.app import App, entrypoint\n"
        "\n"
        "class FetchInput(Input):\n"
        "    x: str = ''\n"
        "\n"
        "class FetchOutput(Output):\n"
        "    y: int = 0\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: FetchInput) -> FetchOutput:\n"
        "        return FetchOutput()\n"
    )
    findings = _scan_one(tmp_path, src)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert p013 == []


def test_p013_silent_on_third_party_unresolvable_annotation(tmp_path: Path) -> None:
    """A boundary type imported from a third-party lib (not in scanned tree) is
    assumed OK — no false-positive."""
    files = {
        "connector.py": (
            _APP_IMPORTS + "from some_third_party import SomeInput, SomeOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: SomeInput) -> SomeOutput:\n"
            "        return SomeOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert p013 == []


def test_p013_silent_on_signal_and_query_methods(tmp_path: Path) -> None:
    """@signal and @query are runtime interactions, not task boundaries → not P013."""
    src = (
        "from application_sdk.app import App, signal, query\n"
        "\n"
        "class MyApp(App):\n"
        "    @signal\n"
        "    async def pause(self, flag: bool) -> None:\n"
        "        pass\n"
        "\n"
        "    @query\n"
        "    def status(self) -> str:\n"
        "        return 'ok'\n"
    )
    findings = _scan_one(tmp_path, src)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert p013 == []


def test_p013_silent_on_non_sdk_entrypoint_decorator(tmp_path: Path) -> None:
    """@flask.entrypoint or other non-SDK decorator → not P013 (provenance excluded)."""
    src = (
        "from flask import entrypoint\n"
        "\n"
        "class MyThing:\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    findings = _scan_one(tmp_path, src)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert p013 == []


def test_p013_implicit_run_fires_on_app_subclass(tmp_path: Path) -> None:
    """Implicit run() override on an App subclass with untyped input → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            "from application_sdk.app import App\n"
            "from contracts import FetchOutput\n"
            "\n"
            "class MyConnector(App):\n"
            "    async def run(self, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


def test_p013_implicit_run_silent_on_non_app_class(tmp_path: Path) -> None:
    """run() on a class that does not subclass App → not P013."""
    src = (
        "class SomeHelper:\n"
        "    async def run(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    findings = _scan_one(tmp_path, src)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert p013 == []


def test_p013_suppression_directive_clears_gate(tmp_path: Path) -> None:
    """A justified inline suppression on a P013 violation keeps gate green.

    The suppression comment must be on the same line as the finding (the ``def``
    line where the annotation lives).  An inline ``# conformance: ignore[P013]``
    on that line always suppresses regardless of ``comment_only`` status.
    """
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: dict) -> FetchOutput:  # conformance: ignore[P013] legacy shim pending contract migration\n"
            "        return FetchOutput()\n"
        ),
    }
    paths = []
    for name, src in files.items():
        p = tmp_path / name
        p.write_text(src)
        paths.append(p)
    findings = [f for f in scan_all(paths, tmp_path) if f.rule_id == "P013"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_p013_is_block_tier() -> None:
    assert get_rule("P013").tier is EnforcementTier.BLOCK


def test_p013_block_violation_fails_gate(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from application_sdk.app import App, entrypoint\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p013_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from application_sdk.app import App, entrypoint\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: dict) -> dict:\n"
        "        return {}\n"
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
    p013_results = [r for r in report.runs[0].results if r.rule_id == "P013"]
    assert len(p013_results) >= 1
    assert all(derive_disposition(r) == Disposition.FAILING for r in p013_results)


# ── P014 UntypedTaskBoundary ──────────────────────────────────────────────────


def test_p014_fires_on_untyped_dict_input(tmp_path: Path) -> None:
    """@task with dict input → P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "input" in p014[0].message


def test_p014_fires_on_untyped_return(tmp_path: Path) -> None:
    """@task with dict return → P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: FetchInput) -> dict:\n"
            "        return {}\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "output" in p014[0].message


def test_p014_fires_on_subscripted_dict_input(tmp_path: Path) -> None:
    """@task with dict[str,str] input → P014 (bounded containers still untyped)."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: dict[str, str]) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1


def test_p014_fires_on_plain_base_model_boundary(tmp_path: Path) -> None:
    """@task whose input is a plain BaseModel (in scanned tree, not Input subclass) → P014."""
    files = {
        "contracts.py": (
            "from pydantic import BaseModel\n"
            "\n"
            "class FetchInput(BaseModel):\n"
            "    connection_id: str = ''\n"
        ),
        "out.py": (
            "from application_sdk.contracts import Output\n"
            "class FetchOutput(Output):\n"
            "    rows: int = 0\n"
        ),
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "from out import FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "FetchInput" in p014[0].message


def test_p014_silent_on_correctly_typed_task(tmp_path: Path) -> None:
    """@task with proper Input/Output subclasses → no P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput, FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert p014 == []


def test_p014_silent_on_non_sdk_task_decorator(tmp_path: Path) -> None:
    """@celery.task with untyped args → not P014 (provenance-excluded)."""
    src = (
        "import celery\n"
        "\n"
        "class Worker:\n"
        "    @celery.task\n"
        "    async def fetch(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    findings = _scan_one(tmp_path, src)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert p014 == []


def test_p014_silent_on_from_celery_import_task(tmp_path: Path) -> None:
    """``from celery import task`` → not P014."""
    src = (
        "from celery import task\n"
        "\n"
        "class Worker:\n"
        "    @task\n"
        "    async def fetch(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    findings = _scan_one(tmp_path, src)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert p014 == []


def test_p014_silent_on_third_party_unresolvable_annotation(tmp_path: Path) -> None:
    """Boundary type from a third-party lib (not in tree) is assumed OK."""
    files = {
        "connector.py": (
            _APP_IMPORTS + "from some_lib import SomeInput, SomeOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: SomeInput) -> SomeOutput:\n"
            "        return SomeOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert p014 == []


def test_p014_is_block_tier() -> None:
    assert get_rule("P014").tier is EnforcementTier.BLOCK


def test_p014_block_violation_fails_gate(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from application_sdk.app import App, task\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p014_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from application_sdk.app import App, task\n"
        "class MyApp(App):\n"
        "    @task\n"
        "    async def fetch(self, input: dict) -> dict:\n"
        "        return {}\n"
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
    p014_results = [r for r in report.runs[0].results if r.rule_id == "P014"]
    assert len(p014_results) >= 1
    assert all(derive_disposition(r) == Disposition.FAILING for r in p014_results)


# ── P013/P014 additional coverage ─────────────────────────────────────────────

# TEST 8 — multi-hop transitive resolution


def test_p013_silent_on_multi_hop_valid_base(tmp_path: Path) -> None:
    """FetchInput(MyBaseInput) where MyBaseInput(Input) across files → no P013."""
    files = {
        "base.py": (
            "from application_sdk.contracts import Input\n"
            "class MyBaseInput(Input):\n"
            "    x: str = ''\n"
        ),
        "contracts.py": (
            "from base import MyBaseInput\n"
            "class FetchInput(MyBaseInput):\n"
            "    y: int = 0\n"
        ),
        "out.py": (
            "from application_sdk.contracts import Output\n"
            "class FetchOutput(Output):\n"
            "    rows: int = 0\n"
        ),
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "from out import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    assert [f for f in findings if f.rule_id == "P013"] == []


def test_p013_fires_on_multi_hop_invalid_base(tmp_path: Path) -> None:
    """FetchInput(MyBaseInput) where MyBaseInput(BaseModel) across files → P013."""
    files = {
        "base.py": (
            "from pydantic import BaseModel\n"
            "class MyBaseInput(BaseModel):\n"
            "    x: str = ''\n"
        ),
        "contracts.py": (
            "from base import MyBaseInput\n"
            "class FetchInput(MyBaseInput):\n"
            "    y: int = 0\n"
        ),
        "out.py": (
            "from application_sdk.contracts import Output\n"
            "class FetchOutput(Output):\n"
            "    rows: int = 0\n"
        ),
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "from out import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "FetchInput" in p013[0].message


# TEST 9 — attribute-form non-SDK entrypoint decorator


def test_p013_silent_on_flask_entrypoint_attribute_form(tmp_path: Path) -> None:
    """import flask; @flask.entrypoint → not P013 (provenance-excluded)."""
    src = (
        "import flask\n"
        "\n"
        "class Foo:\n"
        "    @flask.entrypoint\n"
        "    async def run_it(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    findings = _scan_one(tmp_path, src)
    assert [f for f in findings if f.rule_id == "P013"] == []


# TEST 10 — cycle guard (no RecursionError)


def test_p013_p014_cycle_in_class_hierarchy_no_error(tmp_path: Path) -> None:
    """A cycle A(B)/B(A) in the scanned tree must not cause RecursionError.

    A and B are in the scanned universe but don't reach Input/Output, so
    findings ARE emitted — this test guards against crashes, not false positives.
    """
    src = (
        "from application_sdk.app import App, entrypoint, task\n"
        "\n"
        "class A(B):\n"
        "    pass\n"
        "\n"
        "class B(A):\n"
        "    pass\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: A) -> B:\n"
        "        pass\n"
        "\n"
        "    @task\n"
        "    async def fetch(self, input: B) -> A:\n"
        "        pass\n"
    )
    # Must not raise RecursionError or any exception.
    findings = _scan_one(tmp_path, src)
    # A and B are in tree but don't reach Input/Output → findings emitted.
    assert any(f.rule_id in ("P013", "P014") for f in findings)


# TEST 11 — SDK-contract-import provenance fast-path


def test_p013_silent_when_contracts_imported_directly_from_sdk(
    tmp_path: Path,
) -> None:
    """from application_sdk.contracts import Input, Output → direct SDK import fast-path."""
    src = (
        "from application_sdk.contracts import Input, Output\n"
        "from application_sdk.app import App, entrypoint\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: Input) -> Output:\n"
        "        pass\n"
    )
    findings = _scan_one(tmp_path, src)
    assert [f for f in findings if f.rule_id == "P013"] == []


# TEST 12 — arity-0 entrypoint silently skipped


def test_p013_silent_on_arity_zero_entrypoint(tmp_path: Path) -> None:
    """@entrypoint async def run_it(self) → arity-0, skipped (runtime rejects it)."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    assert [f for f in findings if f.rule_id == "P013"] == []


# TEST 13 — Optional[...] / X | None unwrap for P013


def test_p013_silent_on_optional_valid_input(tmp_path: Path) -> None:
    """Optional[FetchInput] input annotation → valid after unwrap, no P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Optional\n"
            "from contracts import FetchInput, FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: Optional[FetchInput]) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    assert [f for f in findings if f.rule_id == "P013"] == []


def test_p013_fires_on_optional_dict_input(tmp_path: Path) -> None:
    """Optional[dict] input annotation → unwraps to dict, still untyped, P013 fires."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Optional\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: Optional[dict]) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


# TEST 13 continued — Optional for P014


def test_p014_silent_on_optional_valid_input(tmp_path: Path) -> None:
    """Optional[FetchInput] input on @task → valid after unwrap, no P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Optional\n"
            "from contracts import FetchInput, FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: Optional[FetchInput]) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    assert [f for f in findings if f.rule_id == "P014"] == []


def test_p014_fires_on_optional_dict_input(tmp_path: Path) -> None:
    """Optional[dict] input on @task → unwraps to dict, still untyped, P014 fires."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Optional\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: Optional[dict]) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "input" in p014[0].message


# BUG 1 regression — -> None return annotation fires


def test_p013_fires_on_none_return_annotation(tmp_path: Path) -> None:
    """@entrypoint with ``-> None`` return → treated as clearly untyped, P013 fires."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, input: FetchInput) -> None:\n"
            "        pass\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "output" in p013[0].message


def test_p014_fires_on_none_return_annotation(tmp_path: Path) -> None:
    """@task with ``-> None`` return → treated as clearly untyped, P014 fires."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchInput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, input: FetchInput) -> None:\n"
            "        pass\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "output" in p014[0].message


# BUG 3 regression — App aliased as BaseApp still fires for implicit run()


def test_p013_fires_on_implicit_run_with_aliased_app_base(tmp_path: Path) -> None:
    """from application_sdk.app import App as BaseApp; class MyApp(BaseApp) — implicit run() fires."""
    src = (
        "from application_sdk.app import App as BaseApp\n"
        "\n"
        "class MyApp(BaseApp):\n"
        "    async def run(self, input: dict) -> dict:\n"
        "        return {}\n"
    )
    findings = _scan_one(tmp_path, src)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) >= 1


# ── P015 UnmodeledBoundedContractField ────────────────────────────────────────


def _p015_ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, "x.py") if f.rule_id == "P015"]


@pytest.mark.parametrize(
    "annotation",
    [
        "dict",
        "dict[str, str]",
        "Dict[str, Any]",
        "list[str]",
        "List[int]",
        "set[str]",
        "Set[str]",
        "Mapping[str, str]",
        "Sequence[str]",
    ],
)
def test_p015_fires_on_unmodeled_container(annotation: str) -> None:
    """Container of primitives/Any on a contract field → P015."""
    src = f"class MyInput(Input):\n    data: {annotation}\n"
    assert _p015_ids(src) == ["P015"], f"Expected P015 for annotation '{annotation}'"


def test_p015_fires_on_bounded_annotated_dict(tmp_path: Path) -> None:
    """Annotated[dict[str, str], MaxItems(50)] — bounded but unmodeled → P015."""
    src = (
        "from typing import Annotated\n"
        "class MyInput(Input):\n"
        "    data: Annotated[dict[str, str], object()]\n"
    )
    assert _p015_ids(src) == ["P015"]


def test_p015_fires_on_optional_dict(tmp_path: Path) -> None:
    """dict[str, str] | None → P015 (optional wrapper doesn't exempt the container)."""
    src = "class MyInput(Input):\n    data: dict[str, str] | None\n"
    assert _p015_ids(src) == ["P015"]


def test_p015_fires_on_bare_dict_output_field(tmp_path: Path) -> None:
    """Bare dict on an Output contract → P015."""
    src = "class MyOutput(Output):\n    result: dict\n"
    assert _p015_ids(src) == ["P015"]


def test_p015_silent_on_list_of_model(tmp_path: Path) -> None:
    """list[FooModel] — container of a typed class → exempt, no P015."""
    src = (
        "class FooModel:\n"
        "    x: str = ''\n"
        "\n"
        "class MyInput(Input):\n"
        "    items: list[FooModel]\n"
    )
    assert _p015_ids(src) == []


def test_p015_silent_on_dict_str_model(tmp_path: Path) -> None:
    """dict[str, FooModel] — dict of model values → exempt, no P015."""
    src = (
        "class FooModel:\n"
        "    x: str = ''\n"
        "\n"
        "class MyInput(Input):\n"
        "    lookup: dict[str, FooModel]\n"
    )
    assert _p015_ids(src) == []


def test_p015_silent_on_scalar_field(tmp_path: Path) -> None:
    """Non-container scalar field on a contract → no P015."""
    src = (
        "class MyInput(Input):\n"
        "    connection_id: str\n"
        "    limit: int\n"
        "    enabled: bool\n"
    )
    assert _p015_ids(src) == []


def test_p015_silent_on_non_contract_class(tmp_path: Path) -> None:
    """dict[str, str] field on a non-contract class → no P015."""
    src = "class SomeConfig:\n    settings: dict[str, str]\n"
    assert _p015_ids(src) == []


def test_p015_fires_on_optional_annotated_dict(tmp_path: Path) -> None:
    """Optional[Annotated[dict[str, str], MaxItems(50)]] → BUG 2 regression.

    The unwrap loop must handle both Annotated-outer and Optional-outer orderings.
    """
    src = (
        "from typing import Annotated, Optional\n"
        "class FetchInput(Input):\n"
        "    metadata: Optional[Annotated[dict[str, str], 'MaxItems(50)']]\n"
    )
    assert _p015_ids(src) == ["P015"]


def test_p015_silent_on_foreign_output(tmp_path: Path) -> None:
    """A class extending Output imported from a non-SDK module is excluded."""
    src = (
        "from pydantic_ai import Output\n"
        "\n"
        "class MyResult(Output):\n"
        "    data: dict[str, str]\n"
    )
    assert _p015_ids(src) == []


def test_p015_suppression_directive(tmp_path: Path) -> None:
    """# conformance: ignore[P015] suppresses the finding."""
    src = (
        "class MyInput(Input):\n"
        "    # conformance: ignore[P015] schema varies per connector version\n"
        "    data: dict[str, str]\n"
    )
    findings = [f for f in scan_text(src, "x.py") if f.rule_id == "P015"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_p015_is_warn_tier() -> None:
    """P015 is WARN, not BLOCK — a modeling nudge, not a gate failure."""
    assert get_rule("P015").tier is EnforcementTier.WARN


def test_p015_warn_violation_passes_gate(tmp_path: Path) -> None:
    """An unsuppressed P015 (WARN) does not fail the gate (exit 0)."""
    (tmp_path / "m.py").write_text("class MyInput(Input):\n    data: dict[str, str]\n")
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_p015_result_is_warning_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("class MyInput(Input):\n    data: dict[str, str]\n")
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
    p015_results = [r for r in report.runs[0].results if r.rule_id == "P015"]
    assert len(p015_results) >= 1
    assert all(derive_disposition(r) == Disposition.WARNING for r in p015_results)


# ── P013/P014 fixed-point unwrap regressions ──────────────────────────────────


def test_p013_fires_on_optional_annotated_dict_input(tmp_path: Path) -> None:
    """Optional[Annotated[dict[str,str], M]] input → fixed-point loop unwraps both layers → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Annotated, Optional\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(\n"
            "        self, input: Optional[Annotated[dict[str, str], object()]]\n"
            "    ) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


def test_p014_fires_on_optional_annotated_dict_input(tmp_path: Path) -> None:
    """Optional[Annotated[dict[str,str], M]] input on @task → P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Annotated, Optional\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(\n"
            "        self, input: Optional[Annotated[dict[str, str], object()]]\n"
            "    ) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "input" in p014[0].message


def test_p013_fires_on_annotated_optional_dict_input(tmp_path: Path) -> None:
    """Annotated[Optional[dict], M] input → outer Annotated then Optional unwrapped → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from typing import Annotated, Optional\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(\n"
            "        self, input: Annotated[Optional[dict], object()]\n"
            "    ) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


# ── P013/P014 aliased SDK decorator regressions ───────────────────────────────


def test_p013_fires_on_aliased_sdk_entrypoint_decorator(tmp_path: Path) -> None:
    """from application_sdk.app import entrypoint as ep; @ep with dict input → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            "from application_sdk.app import App\n"
            "from application_sdk.app import entrypoint as ep\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @ep\n"
            "    async def run_it(self, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


def test_p014_fires_on_aliased_sdk_task_decorator(tmp_path: Path) -> None:
    """from application_sdk.app import task as sdk_task; @sdk_task with dict input → P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            "from application_sdk.app import App\n"
            "from application_sdk.app import task as sdk_task\n"
            "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @sdk_task\n"
            "    async def fetch(self, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "input" in p014[0].message


# ── P013/P014 keyword-only parameter regressions ──────────────────────────────


def test_p014_fires_on_kwonly_input(tmp_path: Path) -> None:
    """def fetch(self, *, input: dict) — keyword-only param caught by _get_non_self_params → P014."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @task\n"
            "    async def fetch(self, *, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p014 = [f for f in findings if f.rule_id == "P014"]
    assert len(p014) == 1
    assert "input" in p014[0].message


def test_p013_fires_on_kwonly_input(tmp_path: Path) -> None:
    """def run_it(self, *, input: dict) — keyword-only input on @entrypoint → P013."""
    files = {
        "contracts.py": _TYPED_CONTRACTS,
        "connector.py": (
            _APP_IMPORTS + "from contracts import FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run_it(self, *, input: dict) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        ),
    }
    findings = _scan_files(tmp_path, files)
    p013 = [f for f in findings if f.rule_id == "P013"]
    assert len(p013) == 1
    assert "input" in p013[0].message


# ── P026 GetattrOnTypedContractField ───────────────────────────────────────────


def test_p026_fires_on_getattr_with_default_on_typed_param() -> None:
    src = (
        "class MyApp:\n"
        "    @task\n"
        "    async def fetch(self, input: FetchInput) -> FetchOutput:\n"
        '        wid = getattr(input, "workflow_id", "")\n'
        "        return FetchOutput()\n"
    )
    assert "P026" in _ids(src)


def test_p026_fires_on_entrypoint_too() -> None:
    src = (
        "class MyApp:\n"
        "    @entrypoint\n"
        "    async def run_it(self, input: FetchInput) -> FetchOutput:\n"
        '        return getattr(input, "output_path", None)\n'
    )
    assert "P026" in _ids(src)


def test_p026_fires_on_kwonly_input() -> None:
    # def fetch(self, *, input: FetchInput) — keyword-only typed contract param,
    # the canonical form P013/P014 accept; getattr-with-default must still fire.
    src = (
        "class MyApp:\n"
        "    @task\n"
        "    async def fetch(self, *, input: FetchInput) -> FetchOutput:\n"
        '        wid = getattr(input, "workflow_id", "")\n'
        "        return FetchOutput()\n"
    )
    assert "P026" in _ids(src)


def test_p026_no_finding_on_attribute_access() -> None:
    src = (
        "class MyApp:\n"
        "    @task\n"
        "    async def fetch(self, input: FetchInput) -> FetchOutput:\n"
        "        wid = input.workflow_id\n"
        "        return FetchOutput()\n"
    )
    assert "P026" not in _ids(src)


def test_p026_no_finding_on_untyped_param() -> None:
    # input: dict is clearly-untyped — that boundary is P013/P014's concern.
    src = (
        "class MyApp:\n"
        "    @task\n"
        "    async def fetch(self, input: dict) -> FetchOutput:\n"
        '        wid = getattr(input, "workflow_id", "")\n'
        "        return FetchOutput()\n"
    )
    assert "P026" not in _ids(src)


def test_p026_no_finding_without_default() -> None:
    # Two-arg getattr still raises AttributeError — not the silent-substitution hazard.
    src = (
        "class MyApp:\n"
        "    @task\n"
        "    async def fetch(self, input: FetchInput) -> FetchOutput:\n"
        '        wid = getattr(input, "workflow_id")\n'
        "        return FetchOutput()\n"
    )
    assert "P026" not in _ids(src)


def test_p026_no_finding_outside_entrypoint_or_task() -> None:
    src = (
        "class MyApp:\n"
        "    async def helper(self, input: FetchInput) -> FetchOutput:\n"
        '        wid = getattr(input, "workflow_id", "")\n'
        "        return FetchOutput()\n"
    )
    assert "P026" not in _ids(src)


def test_p026_suppressed_inline() -> None:
    src = (
        "class MyApp:\n"
        "    @task\n"
        "    async def fetch(self, input: FetchInput) -> FetchOutput:\n"
        '        wid = getattr(input, "workflow_id", "")  '
        "# conformance: ignore[P026] input is sometimes a raw dict in this legacy path\n"
        "        return FetchOutput()\n"
    )
    fs = [f for f in scan_text(src, "x.py") if f.rule_id == "P026"]
    assert fs and all(f.suppressed for f in fs)


# ── P027 AppStateAsCrossTaskChannel ────────────────────────────────────────────


def _p027_ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings if f.rule_id == "P027"]


def test_p027_fires_on_read_with_no_writer(tmp_path: Path) -> None:
    src = (
        "class MyApp:\n"
        "    def run(self):\n"
        '        return self.get_app_state("auth_header")\n'
    )
    assert _p027_ids(_scan_one(tmp_path, src)) == ["P027"]


def test_p027_fires_when_only_writer_stores_none(tmp_path: Path) -> None:
    # The openapi shape: a "claim ownership" write of None, no real populating write.
    src = (
        "class MyApp:\n"
        "    def run(self):\n"
        '        h = self.get_app_state("auth_header")\n'
        '        self.set_app_state("auth_header", None)\n'
    )
    assert _p027_ids(_scan_one(tmp_path, src)) == ["P027"]


def test_p027_no_finding_when_populating_writer_exists(tmp_path: Path) -> None:
    src = (
        "class MyApp:\n"
        "    def run(self):\n"
        '        cfg = self.get_app_state("cfg")\n'
        "        if cfg is None:\n"
        "            cfg = build()\n"
        '            self.set_app_state("cfg", cfg)\n'
    )
    assert _p027_ids(_scan_one(tmp_path, src)) == []


def test_p027_no_finding_on_dynamic_key(tmp_path: Path) -> None:
    src = (
        "class MyApp:\n"
        "    def run(self, k):\n"
        "        return self.get_app_state(k)\n"
    )
    assert _p027_ids(_scan_one(tmp_path, src)) == []


def test_p027_resolves_key_constant_across_files(tmp_path: Path) -> None:
    # Key defined in one module, read in another, no writer anywhere.
    files = {
        "consts.py": 'AUTH_KEY = "_validated_auth_header"\n',
        "connector.py": (
            "from consts import AUTH_KEY\n"
            "class MyApp:\n"
            "    def run(self):\n"
            "        return self.get_app_state(AUTH_KEY)\n"
        ),
    }
    assert _p027_ids(_scan_files(tmp_path, files)) == ["P027"]


def test_p027_suppressed_inline(tmp_path: Path) -> None:
    src = (
        "class MyApp:\n"
        "    def run(self):\n"
        '        return self.get_app_state("auth_header")  '
        "# conformance: ignore[P027] writer lives in an external base class\n"
    )
    fs = [f for f in _scan_one(tmp_path, src) if f.rule_id == "P027"]
    assert fs and all(f.suppressed for f in fs)


# ── P028 ManualQualifiedNameFString ────────────────────────────────────────────


def test_p028_fires_on_qualified_name_fstring() -> None:
    src = 'db_qn = f"{connection_qualified_name}/{db_name}"\n'
    assert "P028" in _ids(src)


def test_p028_fires_on_qn_chain_variable() -> None:
    # Second link references db_qn (matches *_qn) and has a separator.
    src = 'schema_qn = f"{db_qn}/{schema_name}"\n'
    assert "P028" in _ids(src)


def test_p028_no_finding_without_separator() -> None:
    src = 'label = f"qn is {connection_qualified_name}"\n'
    assert "P028" not in _ids(src)


def test_p028_no_finding_without_qualified_name_ref() -> None:
    src = 'path = f"{base_dir}/{child}"\n'
    assert "P028" not in _ids(src)


def test_p028_suppressed_inline() -> None:
    src = (
        'qn = f"{connection_qualified_name}/{db_name}"  '
        "# conformance: ignore[P028] raw qualifiedName required for a lineage shim\n"
    )
    fs = [f for f in scan_text(src, "x.py") if f.rule_id == "P028"]
    assert fs and all(f.suppressed for f in fs)
