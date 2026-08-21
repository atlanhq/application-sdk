"""Meta-tests for the P-series error-seam checks (P043/P045, CONNECT-970).

P045 flags importing an SDK error class from an internal module; P043 flags
making one load-bearing in control flow.  ``application_sdk.errors.__all__`` is
the public error contract, and a class outside it can move — or stop being the
one a boundary raises — in a minor release.  That is what happened: an app's
``except FormatReadError`` silently stopped matching a bare
``ObjectStoreReadError`` and the guard became dead code.

The tests pin both halves: the rules fire across every shape that makes a class's
identity load-bearing, and stay silent on the public surface and on the helper
*functions* that share these internal modules — ``storage.formats.utils``
exports ``convert_datetime_to_epoch`` and ``process_null_fields``, which apps use
legitimately and which have no public equivalent to move to.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.error_seam import SERIES, discover, scan_text

_PRIVATE_IMPORT = (
    "from application_sdk.storage.formats.format_errors import FormatReadError\n"
)


def _rule(src: str, rule_id: str, file: str = "app/utils/io.py") -> list:
    """Findings of a single rule from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P045 PrivateErrorClassImport — fires ─────────────────────────────────────


def test_p044_fires_on_private_error_import() -> None:
    fs = _rule(_PRIVATE_IMPORT, "P045")
    assert len(fs) == 1 and fs[0].line == 1


def test_p044_emits_one_finding_per_statement_not_per_name() -> None:
    src = (
        "from application_sdk.storage.formats.format_errors import (\n"
        "    FormatReadError,\n"
        "    ObjectStoreReadError,\n"
        ")\n"
    )
    assert len(_rule(src, "P045")) == 1


def test_p044_fires_on_lazy_in_function_import() -> None:
    src = "def f():\n    " + _PRIVATE_IMPORT + "    return FormatReadError\n"
    assert len(_rule(src, "P045")) == 1


def test_p044_message_points_at_the_public_module_for_a_promoted_class() -> None:
    src = (
        "from application_sdk.storage.formats.format_errors import "
        "ObjectStoreReadError\n"
    )
    (finding,) = _rule(src, "P045")
    assert "application_sdk.errors" in finding.message
    assert "Import it from" in finding.message


def test_p044_message_points_at_the_code_branch_when_no_public_class_exists() -> None:
    (finding,) = _rule(_PRIVATE_IMPORT, "P045")
    assert ".code" in finding.message


# ── P045 — stays silent ──────────────────────────────────────────────────────


def test_p044_silent_on_helper_function_imports() -> None:
    """The Snowflake case: six real sites import these two helpers legitimately."""
    src = (
        "from application_sdk.storage.formats.utils import (\n"
        "    convert_datetime_to_epoch,\n"
        "    process_null_fields,\n"
        ")\n"
    )
    assert scan_text(src, "app/pipeline/rolling_writers.py") == []


def test_p044_silent_on_the_public_error_module() -> None:
    src = "from application_sdk.errors import AppError, ObjectStoreReadError\n"
    assert scan_text(src, "app/utils/io.py") == []


def test_p044_silent_on_a_relative_import_of_a_similar_name() -> None:
    src = "from .format_errors import FormatReadError\n"
    assert scan_text(src, "app/utils/io.py") == []


def test_p044_suppressed_inline() -> None:
    src = _PRIVATE_IMPORT.rstrip("\n") + "  # conformance: ignore[P045] reviewed\n"
    fs = _rule(src, "P045")
    assert len(fs) == 1 and fs[0].suppressed
    assert fs[0].suppression_justification == "reviewed"


# ── P043 NonPublicErrorControlFlow — fires on all five shapes ────────────────


def test_p043_fires_on_single_except() -> None:
    src = _PRIVATE_IMPORT + "try:\n    pass\nexcept FormatReadError:\n    raise\n"
    fs = _rule(src, "P043")
    assert len(fs) == 1 and fs[0].line == 4


def test_p043_fires_once_on_an_except_tuple_with_one_covered_name() -> None:
    src = (
        _PRIVATE_IMPORT
        + "try:\n    pass\nexcept (FormatReadError, ValueError):\n    raise\n"
    )
    assert len(_rule(src, "P043")) == 1


def test_p043_fires_per_name_on_an_except_tuple_of_two_covered_names() -> None:
    src = (
        "from application_sdk.storage.formats.format_errors import (\n"
        "    FormatReadError,\n"
        "    FormatWriteError,\n"
        ")\n"
        "try:\n    pass\nexcept (FormatReadError, FormatWriteError):\n    raise\n"
    )
    assert len(_rule(src, "P043")) == 2


def test_p043_fires_on_isinstance() -> None:
    src = _PRIVATE_IMPORT + "def f(e):\n    return isinstance(e, FormatReadError)\n"
    assert len(_rule(src, "P043")) == 1


def test_p043_fires_on_issubclass() -> None:
    src = _PRIVATE_IMPORT + "def f(t):\n    return issubclass(t, FormatReadError)\n"
    assert len(_rule(src, "P043")) == 1


def test_p043_fires_on_subclassing() -> None:
    src = _PRIVATE_IMPORT + "class MyReadError(FormatReadError):\n    pass\n"
    assert len(_rule(src, "P043")) == 1


# ── P043 — stays silent ──────────────────────────────────────────────────────


def test_p043_silent_on_the_public_error_base() -> None:
    src = (
        "from application_sdk.errors import AppError\n"
        "try:\n    pass\nexcept AppError:\n    raise\n"
    )
    assert scan_text(src, "app/utils/io.py") == []


def test_p043_silent_on_a_promoted_class_imported_publicly() -> None:
    src = (
        "from application_sdk.errors import ObjectStoreReadError\n"
        "try:\n    pass\nexcept ObjectStoreReadError:\n    raise\n"
    )
    assert scan_text(src, "app/utils/io.py") == []


def test_promoted_class_on_the_legacy_path_yields_p044_only() -> None:
    """Mid-migration state: a promoted class still imported from the internal
    module is a P045 import-path finding, never a P043 control-flow finding —
    P043's "not exported" claim would be false for it."""
    src = (
        "from application_sdk.storage.formats.format_errors import "
        "ObjectStoreReadError\n"
        "try:\n    pass\nexcept ObjectStoreReadError:\n    raise\n"
    )
    findings = scan_text(src, "app/utils/io.py")
    assert [f.rule_id for f in findings] == ["P045"]


def test_p043_silent_on_a_non_error_sdk_base_class() -> None:
    """Without the Error-suffix guard this flags every App and Input subclass."""
    src = "from application_sdk.app import App\nclass MyApp(App):\n    pass\n"
    assert scan_text(src, "app/main.py") == []


def test_p043_silent_on_a_bare_annotation() -> None:
    """An annotation changes no behaviour; P045 already covers the import."""
    src = _PRIVATE_IMPORT + "def f() -> FormatReadError | None:\n    return None\n"
    assert _rule(src, "P043") == []


def test_p043_silent_on_an_app_owned_class_of_the_same_name() -> None:
    src = (
        "from app.errors import FormatReadError\n"
        "try:\n    pass\nexcept FormatReadError:\n    raise\n"
    )
    assert scan_text(src, "app/utils/io.py") == []


def test_p043_suppressed_inline() -> None:
    src = (
        _PRIVATE_IMPORT
        + "try:\n    pass\nexcept FormatReadError:  # conformance: ignore[P043] reviewed\n"
        "    raise\n"
    )
    fs = _rule(src, "P043")
    assert len(fs) == 1 and fs[0].suppressed
    assert fs[0].suppression_justification == "reviewed"


# ── The incident shape, end to end ──────────────────────────────────────────


def test_reproduces_the_connect_970_shape() -> None:
    """The real code from the incident: one import line, one dead guard.

    Both imports are one P045 statement.  On the control-flow side,
    ``FormatReadError`` (still internal) draws a P043, but the promoted
    ``ObjectStoreReadError`` does not — P043's "not exported" claim would be
    false for it, and P045 already owns the import-path migration.
    """
    src = (
        "from application_sdk.storage.formats.format_errors import (\n"
        "    FormatReadError,\n"
        "    ObjectStoreReadError,\n"
        ")\n"
        "\n"
        "def read_json(path):\n"
        "    try:\n"
        "        return _read(path)\n"
        "    except FormatReadError as exc:\n"
        "        if not is_empty_prefix_error(exc):\n"
        "            raise\n"
        "        return []\n"
        "\n"
        "def is_empty_prefix_error(exc):\n"
        "    return isinstance(exc, ObjectStoreReadError)\n"
    )
    assert len(_rule(src, "P045")) == 1
    assert len(_rule(src, "P043")) == 1
    (p043,) = _rule(src, "P043")
    assert "FormatReadError" in p043.message


# ── Discovery ────────────────────────────────────────────────────────────────


def test_discover_includes_test_files(tmp_path: Path) -> None:
    """The incident froze the superseded shape into a fixture that passed forever."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "io.py").write_text(_PRIVATE_IMPORT, encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_utils_io.py").write_text(
        _PRIVATE_IMPORT, encoding="utf-8"
    )

    found = {p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)}

    assert "app/io.py" in found
    assert "tests/unit/test_utils_io.py" in found


def test_discover_skips_dot_directories(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "mod.py").write_text(_PRIVATE_IMPORT, encoding="utf-8")

    assert discover(tmp_path) == []


# ── Catalog registration ─────────────────────────────────────────────────────


def test_rules_are_registered_as_app_scoped_warnings() -> None:
    from conformance.suite.rules import CATALOG
    from conformance.suite.schema.disposition import EnforcementTier, RuleScope

    for rule_id in ("P043", "P045"):
        rule = CATALOG[rule_id]
        assert rule.scope is RuleScope.APP
        assert rule.tier is EnforcementTier.WARN
        assert rule.category == "error-seam"
