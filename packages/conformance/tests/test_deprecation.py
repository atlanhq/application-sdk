"""Meta-tests for the B-series deprecation checks (B001–B004).

These checks fan out across the fleet — a buggy check false-positives across many
apps and triggers spurious remediations.  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false negatives
are guarded.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _parse_directives
from conformance.suite.checks._version import parse_version, version_reached
from conformance.suite.checks.deprecation._authoring import scan_authoring
from conformance.suite.checks.deprecation._consumer import scan_consumer
from conformance.suite.checks.deprecation._extractor import (
    extract_notices,
    extract_sites,
)
from conformance.suite.checks.deprecation._manifest import (
    DeprecatedSymbol,
    Manifest,
    load_manifest,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope


def _tree_and_directives(src: str) -> tuple[ast.Module, dict]:
    return ast.parse(src), _parse_directives(src)


# A small fixture manifest for the consumer (B001) tests.
_MANIFEST = Manifest(
    symbols=(
        DeprecatedSymbol(
            symbol="DiscoveryError",
            kind="class",
            module="application_sdk.discovery",
            marker_via="warn",
            message="DiscoveryError is deprecated; use InvalidInputError — removed in v4.0",
            migration_target=True,
            removal_version="4.0",
        ),
        DeprecatedSymbol(
            symbol="BaseMetadataExtractor",
            kind="class",
            module="application_sdk.templates.base_metadata_extractor",
            marker_via="warn",
            message="Use application_sdk.templates.SqlApp instead. Will be removed in v4.0.0.",
            migration_target=True,
            removal_version="4.0.0",
        ),
        DeprecatedSymbol(
            symbol="upload_to_atlan",
            kind="method",
            module="application_sdk.templates.base_metadata_extractor",
            marker_via="decorator",
            message="upload_to_atlan is deprecated. Migrate to App.upload(...).",
            migration_target=True,
            removal_version=None,
        ),
        DeprecatedSymbol(
            symbol="DataframeType.daft",
            kind="enum_member",
            module="application_sdk.common.types",
            marker_via="enum-member",
            message=(
                "DataframeType.daft is deprecated; use DataframeType.pandas "
                "instead — will be removed in v4.0.0."
            ),
            migration_target=True,
            removal_version="4.0.0",
        ),
    ),
)


def _b001(src: str) -> list:
    tree, directives = _tree_and_directives(src)
    return scan_consumer(tree, "x.py", _MANIFEST, directives)


def _authoring_ids(src: str, version: str | None = "3.18.0") -> list[str]:
    tree, directives = _tree_and_directives(src)
    return [f.rule_id for f in scan_authoring(tree, "x.py", version, directives)]


# ── B001 DeprecatedSdkSymbolUsage (consumer / app scope) ────────────────────────


def test_b001_fires_on_deprecated_import() -> None:
    src = "from application_sdk.discovery import DiscoveryError\n"
    findings = _b001(src)
    assert [f.rule_id for f in findings] == ["B001"]
    assert "DiscoveryError" in findings[0].message
    # The SDK's migration guidance rides along for the remediation loop.
    assert "InvalidInputError" in findings[0].message


def test_b001_silent_on_non_deprecated_import() -> None:
    src = "from application_sdk.errors import AppError\n"
    assert _b001(src) == []


def test_b001_silent_on_same_name_from_other_package() -> None:
    # Name-anchored within application_sdk: a same-named symbol elsewhere is safe.
    src = "from mypkg.discovery import DiscoveryError\n"
    assert _b001(src) == []


def test_b001_fires_on_subclassing_deprecated_base() -> None:
    src = (
        "from application_sdk.templates.base_metadata_extractor import "
        "BaseMetadataExtractor\n\n"
        "class MyExtractor(BaseMetadataExtractor):\n    pass\n"
    )
    # One finding for the import, one for the subclass.
    ids = [f.rule_id for f in _b001(src)]
    assert ids == ["B001", "B001"]


def test_b001_fires_on_deprecated_method_call() -> None:
    src = "def run(extractor):\n    return extractor.upload_to_atlan(data)\n"
    findings = _b001(src)
    assert [f.rule_id for f in findings] == ["B001"]
    assert "upload_to_atlan" in findings[0].message


def test_b001_fires_on_deprecated_enum_member() -> None:
    src = (
        "from application_sdk.common.types import DataframeType\n"
        "t = DataframeType.daft\n"
    )
    findings = _b001(src)
    assert [f.rule_id for f in findings] == ["B001"]
    assert "DataframeType.daft" in findings[0].message
    assert "DataframeType.pandas" in findings[0].message


def test_b001_enum_member_respects_import_alias() -> None:
    src = (
        "from application_sdk.common.types import DataframeType as DfType\n"
        "t = DfType.daft\n"
    )
    assert [f.rule_id for f in _b001(src)] == ["B001"]


def test_b001_enum_member_matches_through_a_parent_package_reexport() -> None:
    # The manifest records application_sdk.common.types; importing from the
    # re-exporting parent must still match, as it does for classes.
    src = "from application_sdk.common import DataframeType\nt = DataframeType.daft\n"
    assert [f.rule_id for f in _b001(src)] == ["B001"]


def test_b001_enum_member_matches_module_qualified_access() -> None:
    src = (
        "import application_sdk.common.types as types\n"
        "t = types.DataframeType.daft\n"
    )
    assert [f.rule_id for f in _b001(src)] == ["B001"]


def test_b001_silent_on_non_deprecated_enum_member() -> None:
    src = (
        "from application_sdk.common.types import DataframeType\n"
        "t = DataframeType.pandas\n"
    )
    assert _b001(src) == []


def test_b001_enum_member_is_module_aware() -> None:
    """An app's own same-named enum must not pick up the SDK's deprecated members.

    The member name alone (``daft``) is meaningless out of context, which is why
    the manifest stores the qualified name and matching goes through the enum
    class's import.
    """
    src = "from app.types import DataframeType\nt = DataframeType.daft\n"
    assert _b001(src) == []


def test_b001_enum_member_suppressed_inline() -> None:
    src = (
        "from application_sdk.common.types import DataframeType\n"
        "t = DataframeType.daft  # conformance: ignore[B001] pinned to old reader\n"
    )
    findings = _b001(src)
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_b001_suppressed_inline() -> None:
    src = (
        "from application_sdk.discovery import DiscoveryError  "
        "# conformance: ignore[B001] migration scheduled next sprint\n"
    )
    findings = _b001(src)
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_b001_empty_manifest_is_silent() -> None:
    tree, directives = _tree_and_directives(
        "from application_sdk.discovery import DiscoveryError\n"
    )
    empty = Manifest(symbols=())
    assert scan_consumer(tree, "x.py", empty, directives) == []


def test_b001_module_aware_match_with_fixture() -> None:
    # The deprecated symbol lives in a specific module; importing the *same name*
    # from a sibling module is the recommended replacement and must stay silent.
    manifest = Manifest(
        symbols=(
            DeprecatedSymbol(
                symbol="AppError",
                kind="class",
                module="application_sdk.app.base",
                marker_via="warn",
                message="use application_sdk.errors.AppError — removed in v4.0",
                migration_target=True,
                removal_version="4.0",
            ),
        )
    )

    def ids(src: str) -> list[str]:
        tree, directives = _tree_and_directives(src)
        return [f.rule_id for f in scan_consumer(tree, "x.py", manifest, directives)]

    # Deprecated module (or a submodule of it) → flagged.
    assert ids("from application_sdk.app import AppError\n") == ["B001"]
    # Recommended replacement, same bare name, different module → silent.
    assert ids("from application_sdk.errors import AppError\n") == []


# ── B001 against the REAL committed manifest (collision regression) ─────────────


def test_b001_real_manifest_does_not_flag_recommended_app_error() -> None:
    """Guard the AppError name collision against the *real* manifest, not a fixture.

    ``application_sdk.app.AppError`` is deprecated; its replacement
    ``application_sdk.errors.AppError`` shares the bare name and is the normal
    base every app subclasses.  Name-only matching would fire on essentially
    every consumer — the exact mass-false-positive this rule must avoid.
    """
    manifest = load_manifest()
    src = (
        "from application_sdk.errors import AppError\n\n\n"
        "class MyError(AppError):\n    pass\n"
    )
    tree, directives = _tree_and_directives(src)
    assert scan_consumer(tree, "app/errors.py", manifest, directives) == []


def test_b001_real_manifest_flags_deprecated_discovery_error() -> None:
    """Sanity: the real manifest still fires on a genuinely deprecated import."""
    manifest = load_manifest()
    tree, directives = _tree_and_directives(
        "from application_sdk.discovery import DiscoveryError\n"
    )
    ids = [f.rule_id for f in scan_consumer(tree, "app/x.py", manifest, directives)]
    assert ids == ["B001"]


def test_b001_real_manifest_flags_legacy_transformers() -> None:
    """BLDX-1399: the real manifest fires on the legacy transformer surface.

    Importing / subclassing ``AtlasTransformer`` / ``QueryBasedTransformer`` /
    ``TransformerInterface`` is the YAML/Daft transformer path we are steering
    apps off; B001 must surface it with the asset-mapper migration guidance the
    SDK's deprecation notice carries.
    """
    manifest = load_manifest()
    src = (
        "from application_sdk.transformers.atlas import AtlasTransformer\n"
        "from application_sdk.transformers.query import QueryBasedTransformer\n\n\n"
        "class MyTransformer(AtlasTransformer):\n    pass\n"
    )
    tree, directives = _tree_and_directives(src)
    findings = scan_consumer(tree, "app/connector.py", manifest, directives)
    # two imports + one subclass
    assert [f.rule_id for f in findings] == ["B001", "B001", "B001"]
    # the asset-mapper migration target rides along for the remediation loop
    assert all("asset-mapper" in f.message for f in findings)
    assert all("v4.0" in f.message for f in findings)


def test_b001_real_manifest_flags_transformer_interface_import() -> None:
    """The ABC itself is flagged — an app subclassing it directly is the target."""
    manifest = load_manifest()
    src = "from application_sdk.transformers import TransformerInterface\n"
    tree, directives = _tree_and_directives(src)
    ids = [f.rule_id for f in scan_consumer(tree, "app/x.py", manifest, directives)]
    assert ids == ["B001"]


def test_b001_real_manifest_transformer_suppressible() -> None:
    """A justified inline suppression silences the transformer finding (and is
    still emitted to SARIF in its own category by the runner)."""
    manifest = load_manifest()
    src = (
        "from application_sdk.transformers.atlas import AtlasTransformer  "
        "# conformance: ignore[B001] legacy compat shim\n"
    )
    tree, directives = _tree_and_directives(src)
    findings = scan_consumer(tree, "app/x.py", manifest, directives)
    # The finding is still emitted (audit trail) but marked suppressed, so the
    # runner counts it in its own SUPPRESSED category, not as a live WARNING.
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── B002 MalformedDeprecationNotice (sdk scope) ─────────────────────────────────


def test_b002_fires_when_removal_version_missing() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old() is deprecated; use new()', DeprecationWarning)\n"
    )
    assert _authoring_ids(src) == ["B002"]


def test_b002_fires_when_migration_target_missing() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old() is deprecated, removed in v9.0', DeprecationWarning)\n"
    )
    assert _authoring_ids(src) == ["B002"]


def test_b002_silent_on_well_formed_notice() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old() is deprecated; use new() — removed in v9.0', "
        "DeprecationWarning)\n"
    )
    assert _authoring_ids(src) == []


def test_b002_silent_on_non_deprecation_warning() -> None:
    # A non-DeprecationWarning warn is not a deprecation notice.
    src = "import warnings\ndef f():\n    warnings.warn('something', UserWarning)\n"
    assert _authoring_ids(src) == []


def test_b002_recognises_category_keyword() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old is deprecated', category=DeprecationWarning)\n"
    )
    assert _authoring_ids(src) == ["B002"]


def test_b002_suppressed_inline() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old() is deprecated; use new()', DeprecationWarning)  "
        "# conformance: ignore[B002] message intentionally terse\n"
    )
    tree, directives = _tree_and_directives(src)
    findings = scan_authoring(tree, "x.py", "3.18.0", directives)
    assert [f.rule_id for f in findings] == ["B002"]
    assert findings[0].suppressed is True


# ── B003 OverdueDeprecationRemoval (sdk scope) ──────────────────────────────────


def test_b003_fires_when_removal_version_reached() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old is deprecated; use new — removed in v3.2.0', "
        "DeprecationWarning)\n"
    )
    assert _authoring_ids(src, version="3.18.0") == ["B003"]


def test_b003_silent_when_removal_in_future() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old is deprecated; use new — removed in v4.0', "
        "DeprecationWarning)\n"
    )
    assert _authoring_ids(src, version="3.18.0") == []


def test_b003_skipped_when_version_unknown() -> None:
    src = (
        "import warnings\n"
        "def old():\n"
        "    warnings.warn('old is deprecated; use new — removed in v3.2.0', "
        "DeprecationWarning)\n"
    )
    # No version -> overdue-ness undecidable, B003 silent (B002 also silent: well-formed).
    assert _authoring_ids(src, version=None) == []


def test_b003_fires_on_parameter_deprecation() -> None:
    # The canonical case: a deprecated *parameter* whose removal version has passed.
    src = (
        "import warnings\n"
        "def serve(state_store=None):\n"
        "    if state_store:\n"
        "        warnings.warn('state_store is deprecated; use vault. "
        "Will be removed in v3.2.0.', DeprecationWarning)\n"
    )
    assert _authoring_ids(src, version="3.18.0") == ["B003"]


# ── B004 UnmarkedDeprecationClaim (sdk scope) ───────────────────────────────────


def test_b004_fires_on_unmarked_class_claim() -> None:
    src = (
        "class Old:\n"
        '    """Deprecated: use New instead — removed in v4.0."""\n'
        "    pass\n"
    )
    assert _authoring_ids(src) == ["B004"]


def test_b004_silent_when_decorator_present() -> None:
    src = (
        "from typing_extensions import deprecated\n\n"
        "@deprecated('use New instead — removed in v4.0')\n"
        "class Old:\n"
        '    """Deprecated: use New instead."""\n'
        "    pass\n"
    )
    # Decorator notice is well-formed, so no B002/B003 either.
    assert _authoring_ids(src) == []


def test_b004_silent_when_body_emits_warning() -> None:
    # Claims deprecation AND enforces it via a body warn -> not "unmarked".
    src = (
        "import warnings\n"
        "def old():\n"
        '    """Deprecated: use new() — removed in v4.0."""\n'
        "    warnings.warn('old() is deprecated; use new() — removed in v4.0', "
        "DeprecationWarning)\n"
    )
    assert _authoring_ids(src) == []


def test_b004_silent_without_docstring_claim() -> None:
    src = 'class Fine:\n    """A perfectly current class."""\n    pass\n'
    assert _authoring_ids(src) == []


def test_b004_silent_on_prose_starting_with_deprecated_word() -> None:
    # Free-form prose whose line merely starts with "Deprecated" (no colon) is
    # not a claim — the regex requires the SDK's "Deprecated:" convention.
    src = (
        "class Registry:\n"
        '    """Filters APIs by status.\n\n'
        "    Deprecated APIs are filtered out of the public listing here.\n"
        '    """\n'
        "    pass\n"
    )
    assert _authoring_ids(src) == []


def test_b004_silent_on_field_named_deprecated() -> None:
    # The `deprecated: bool` field trap must not be read as a claim.
    src = (
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Config:\n"
        '    """Live config."""\n'
        "    deprecated: bool = False\n"
    )
    assert _authoring_ids(src) == []


def test_b004_suppressed_inline() -> None:
    src = (
        "# conformance: ignore[B004] doc-only legacy note, intentional\n"
        "class Old:\n"
        '    """Deprecated: use New instead."""\n'
        "    pass\n"
    )
    tree, directives = _tree_and_directives(src)
    findings = scan_authoring(tree, "x.py", "3.18.0", directives)
    assert [f.rule_id for f in findings] == ["B004"]
    assert findings[0].suppressed is True


# ── Extractor + version helpers ─────────────────────────────────────────────────


def test_extractor_finds_decorated_method() -> None:
    src = (
        "from typing_extensions import deprecated\n\n"
        "class A:\n"
        "    @deprecated('gone soon')\n"
        "    def m(self):\n"
        "        pass\n"
    )
    sites = extract_sites(ast.parse(src))
    method = next(s for s in sites if s.symbol == "m")
    assert method.kind == "method"
    assert method.marker_via == "decorator"


def test_extractor_finds_qualified_decorator() -> None:
    # Qualified form (@te.deprecated / @warnings.deprecated) must be recognised,
    # not just the bare-name @deprecated.
    src = (
        "import typing_extensions as te\n\n"
        "@te.deprecated('gone soon — removed in v9.0')\n"
        "def old():\n"
        "    pass\n"
    )
    sites = extract_sites(ast.parse(src))
    assert any(s.symbol == "old" and s.marker_via == "decorator" for s in sites)


def test_removal_version_takes_last_match() -> None:
    from conformance.suite.checks.deprecation._extractor import removal_version

    msg = "removed in v2.0 from internals; will be removed in v4.0 for callers"
    assert removal_version(msg) == "4.0"


def test_extractor_attributes_init_subclass_warn_to_class() -> None:
    src = (
        "import warnings\n"
        "class Base:\n"
        "    def __init_subclass__(cls, **kw):\n"
        "        warnings.warn('Base is deprecated; use New — removed in v4.0', "
        "DeprecationWarning)\n"
    )
    sites = extract_sites(ast.parse(src))
    assert any(s.symbol == "Base" and s.marker_via == "warn" for s in sites)


def test_extract_notices_walks_method_bodies() -> None:
    src = (
        "import warnings\n"
        "class A:\n"
        "    def m(self):\n"
        "        warnings.warn('p is deprecated', DeprecationWarning)\n"
    )
    notices = extract_notices(ast.parse(src))
    assert len(notices) == 1


# ── __deprecated_members__ (the enum-member convention) ────────────────────────


_ENUM_SRC = (
    "from enum import Enum\n\n"
    "class Codec(Enum):\n"
    "    __deprecated_members__ = {\n"
    '        "legacy": "Codec.legacy is deprecated; use Codec.modern — will be '
    'removed in v5.0.0.",\n'
    "    }\n\n"
    '    modern = "modern"\n'
    '    legacy = "legacy"\n'
)


def test_extractor_finds_deprecated_enum_member() -> None:
    site = next(
        s for s in extract_sites(ast.parse(_ENUM_SRC)) if s.kind == "enum_member"
    )
    assert site.symbol == "Codec.legacy"
    assert site.marker_via == "enum-member"
    assert site.removal_version_raw == "5.0.0"
    assert site.has_migration_target is True


def test_deprecated_members_mapping_is_not_itself_a_symbol() -> None:
    # The dunder is class metadata, not a member: EnumMeta skips it and so must
    # the extractor, or the manifest would carry a phantom symbol.
    symbols = {s.symbol for s in extract_sites(ast.parse(_ENUM_SRC))}
    assert "__deprecated_members__" not in symbols
    assert "Codec.modern" not in symbols


def test_enum_member_notice_is_subject_to_authoring_hygiene() -> None:
    # The convention buys B002/B003 for free: an entry that names no removal
    # version is malformed the same way a @deprecated message would be.
    src = (
        "from enum import Enum\n\n"
        "class Codec(Enum):\n"
        '    __deprecated_members__ = {"legacy": "Codec.legacy is deprecated."}\n\n'
        '    legacy = "legacy"\n'
    )
    assert "B002" in _authoring_ids(src)


def test_enum_member_notice_removal_can_fall_overdue() -> None:
    src = (
        "from enum import Enum\n\n"
        "class Codec(Enum):\n"
        "    __deprecated_members__ = {\n"
        '        "legacy": "Codec.legacy is deprecated; use Codec.modern — will '
        'be removed in v2.0.",\n'
        "    }\n\n"
        '    legacy = "legacy"\n'
    )
    assert "B003" in _authoring_ids(src, version="3.18.0")


def test_extractor_ignores_non_literal_deprecated_members_entries() -> None:
    # A computed key names no member we could report and a computed message has
    # no notice text to grade, so neither is recorded.
    src = (
        "from enum import Enum\n\n"
        "KEY = 'legacy'\n"
        "class Codec(Enum):\n"
        "    __deprecated_members__ = {KEY: 'gone', 'other': SOME_CONST}\n\n"
        '    legacy = "legacy"\n'
    )
    assert [s for s in extract_sites(ast.parse(src)) if s.kind == "enum_member"] == []


def test_sdk_manifest_carries_the_dataframe_type_member() -> None:
    # End-to-end: the committed manifest is what B001 reads fleet-wide, so the
    # convention is only real if the generated artifact carries the entry.
    from conformance.suite.checks.deprecation._manifest import load_manifest

    record = next(
        s for s in load_manifest().symbols if s.symbol == "DataframeType.daft"
    )
    assert record.kind == "enum_member"
    assert record.module == "application_sdk.common.types"
    assert record.removal_version == "4.0.0"


def test_load_manifest_warns_on_malformed_json(tmp_path, capsys) -> None:
    # A corrupted committed manifest must degrade to empty *and* surface a
    # stderr warning, so a packaging bug isn't silently invisible fleet-wide.
    bad = tmp_path / "deprecated_symbols.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    manifest = load_manifest(bad)
    assert manifest.symbols == ()
    assert "malformed" in capsys.readouterr().err.lower()


def test_load_manifest_silent_when_absent(tmp_path, capsys) -> None:
    # An absent manifest is graceful (older wheel) — empty, no warning noise.
    manifest = load_manifest(tmp_path / "nope.json")
    assert manifest.symbols == ()
    assert capsys.readouterr().err == ""


def test_parse_version_and_reached() -> None:
    assert parse_version("v3.2.0") == (3, 2, 0)
    assert parse_version("4.0") == (4, 0)
    assert parse_version("not a version") is None
    assert version_reached((3, 2, 0), (3, 18, 0)) is True
    assert version_reached((4, 0), (3, 18, 0)) is False
    assert version_reached((3, 2), (3, 2, 0)) is True


# ── B007 DaftOnlyDataframeApiUsage (consumer / app scope) ───────────────────────

from conformance.suite.checks.deprecation._daft_runtime import (  # noqa: E402
    scan_daft_runtime,
)

_SDK_IMPORT = "from application_sdk.io import ParquetFileReader\n"


def _b007(src: str) -> list:
    tree, directives = _tree_and_directives(src)
    return scan_daft_runtime(tree, "x.py", directives)


def test_b007_fires_on_count_rows() -> None:
    src = _SDK_IMPORT + "n = dataframe.count_rows()\n"
    findings = _b007(src)
    assert [f.rule_id for f in findings] == ["B007"]
    assert "count_rows" in findings[0].message
    assert "len(frame)" in findings[0].message


def test_b007_fires_on_to_pylist_on_reader_frame() -> None:
    src = _SDK_IMPORT + "records = dataframe.to_pylist()\n"
    findings = _b007(src)
    assert [f.rule_id for f in findings] == ["B007"]
    assert 'to_dict("records")' in findings[0].message


def test_b007_exempts_to_pylist_on_pyarrow_table() -> None:
    # pyarrow.Table.to_pylist() is a real API — the SDK itself uses it.
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "table = pa.Table.from_pandas(df)\n"
        + "rows = table.to_pylist()\n"
        + "direct = db.execute(sql).to_arrow_table().to_pylist()\n"
    )
    assert _b007(src) == []


def test_b007_fires_on_names_attribute() -> None:
    src = _SDK_IMPORT + "cols = dataframe.names\n"
    findings = _b007(src)
    assert [f.rule_id for f in findings] == ["B007"]
    assert "frame.columns" in findings[0].message


def test_b007_exempts_attribute_chain_names() -> None:
    # pyarrow's schema.names and pandas' index.names are legitimate chains;
    # self.names is the app's own attribute.
    src = (
        _SDK_IMPORT
        + "a = dataframe.schema.names\n"
        + "b = frame.index.names\n"
        + "c = self.names\n"
    )
    assert _b007(src) == []


def test_b007_no_longer_owns_dataframetype_daft() -> None:
    """The enum member moved to the generated manifest, so B007 must stay silent.

    Leaving the hand-coded copy in place would double-report the one line
    alongside B001 and reopen the drift the manifest's byte-gate prevents.
    """
    src = (
        "from application_sdk.common.types import DataframeType\n"
        "t = DataframeType.daft\n"
    )
    assert _b007(src) == []


def test_b007_silent_without_sdk_import() -> None:
    # A standalone daft script is not consuming SDK reader frames.
    src = "import daft\nn = df.count_rows()\ncols = df.names\n"
    assert _b007(src) == []


def test_b007_silent_on_unrelated_daft_attribute() -> None:
    # .daft on a non-DataframeType receiver was never the enum alias, and is
    # not one now that B001 owns the member.
    src = _SDK_IMPORT + "x = config.daft\n"
    assert _b007(src) == []


def test_b007_suppressed_inline() -> None:
    src = (
        _SDK_IMPORT
        + "# conformance: ignore[B007] receiver is a daft frame from a local path\n"
        + "n = frame.count_rows()\n"
    )
    findings = _b007(src)
    assert len(findings) == 1
    assert findings[0].suppressed


# ── Regression: the pyarrow exemption must not cross function scopes ─────────


def test_b007_pyarrow_exemption_is_scoped_per_function() -> None:
    """A pyarrow-bound `df` in one function must not exempt a real SDK reader
    frame of the same name in another.

    Collected module-wide, the guard erased the very findings it exists to
    protect: `df`, `table`, `data`, `result` are exactly the names that recur
    across functions in real connector modules.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def unrelated():\n"
        + "    df = pa.table({})\n"
        + "    return df.to_pylist()\n"
        + "\n"
        + "def process_reader_output(frame):\n"
        + "    df = frame\n"
        + "    return df.to_pylist()\n"
    )
    findings = _b007(src)
    assert [f.rule_id for f in findings] == ["B007"]
    assert findings[0].line == 10


def test_b007_pyarrow_exemption_still_applies_within_its_own_scope() -> None:
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def helper():\n"
        + "    df = pa.table({})\n"
        + "    return df.to_pylist()\n"
    )
    assert _b007(src) == []


def test_b007_pyarrow_exemption_reaches_a_closure() -> None:
    """A nested function can legitimately see an enclosing binding."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def outer():\n"
        + "    table = pa.table({})\n"
        + "\n"
        + "    def inner():\n"
        + "        return table.to_pylist()\n"
        + "\n"
        + "    return inner\n"
    )
    assert _b007(src) == []


def test_b007_rule_metadata() -> None:
    """WARN is what keeps a new rule off the dogfooded gate — assert it per rule."""
    rule = get_rule("B007")
    assert rule.name == "DaftOnlyDataframeApiUsage"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.rationale.strip()


def test_b007_class_body_binding_does_not_leak_into_methods() -> None:
    """Real Python scoping never lets a method see a class-body name.

    Folding class bodies into the module scope let one `df = pa.table({})` in a
    class body exempt every same-named receiver in every method of the file.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "class Foo:\n"
        + "    df = pa.table({})\n"
        + "\n"
        + "    def method(self, frame):\n"
        + "        df = frame\n"
        + "        return df.to_pylist()\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_rebinding_a_pyarrow_name_voids_the_exemption() -> None:
    """The last binding before the use decides, not "bound anywhere in scope"."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f(frame):\n"
        + "    df = pa.table({})\n"
        + "    df = frame\n"
        + "    return df.to_pylist()\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_exemption_holds_before_a_later_rebind() -> None:
    """Order-awareness must not break the ordinary in-scope exemption."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f(frame):\n"
        + "    df = pa.table({})\n"
        + "    rows = df.to_pylist()\n"
        + "    df = frame\n"
        + "    return rows\n"
    )
    assert _b007(src) == []


def test_b007_closure_defined_before_its_binding_is_exempt() -> None:
    """A closure body runs at call time, not where it is written.

    Applying the line filter when walking OUT to an enclosing scope flagged
    correct code.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def outer():\n"
        + "    def inner():\n"
        + "        return table.to_pylist()\n"
        + "    table = pa.table({})\n"
        + "    return inner()\n"
    )
    assert _b007(src) == []


def test_b007_tracks_every_rebinding_form() -> None:
    """A stale pyarrow exemption must not survive a non-`Assign` rebinding.

    Tracking only `ast.Assign` reopened the round-2 false-negative class through
    walrus, loop and context-manager targets.
    """
    prelude = _SDK_IMPORT + "import pyarrow as pa\n\n"
    for label, body in (
        ("walrus", "    if (df := frame):\n        pass\n"),
        ("for", "    for df in pages:\n        pass\n"),
        ("with", "    with frame as df:\n        pass\n"),
    ):
        src = (
            prelude
            + "def f(frame, pages):\n"
            + "    df = pa.table({})\n"
            + body
            + "    return df.to_pylist()\n"
        )
        assert [f.rule_id for f in _b007(src)] == ["B007"], label


def test_b007_comprehension_over_pyarrow_tables_is_exempt() -> None:
    """`to_pylist()` on a real pyarrow Table is the non-deprecated API.

    Untracked binding forms defaulted to "not pyarrow-bound", which over-reported
    a genuinely-pyarrow comprehension target.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f():\n"
        + "    tables = [pa.table({}) for _ in range(3)]\n"
        + "    return [t.to_pylist() for t in tables]\n"
    )
    assert _b007(src) == []


def test_b007_comprehension_over_reader_frames_still_fires() -> None:
    """The exemption must not swallow the ordinary case."""
    src = (
        _SDK_IMPORT
        + "def f(frames):\n"
        + "    return [t.to_pylist() for t in frames]\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_pyarrow_iterable_in_a_sibling_scope_does_not_exempt() -> None:
    """A pyarrow ``tables`` binding in one function exempts nothing in another.

    The iterable-name lookup scanned every scope's bindings, so
    ``tables = [pa.table({}) ...]`` in ``g`` cleared
    ``[t.to_pylist() for t in tables]`` in ``f`` — where ``tables`` is an SDK
    reader frame and the call is a real B007 violation. Bindings are collected
    per scope precisely so generic names cannot leak exemptions across
    functions; the lookup now walks only the comprehension's own scope chain.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def g():\n"
        + "    tables = [pa.table({}) for _ in range(3)]\n"
        + "    return tables\n"
        + "\n"
        + "def f(tables):\n"
        + "    return [t.to_pylist() for t in tables]\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_parameter_shadowing_a_module_pyarrow_binding_does_not_exempt() -> None:
    """A same-named function parameter kills an enclosing pyarrow exemption.

    ``_pyarrow_bindings_by_scope`` collected assignments only, so walking
    outward from ``f``'s body found the module-level
    ``tables = [pa.table({}) ...]`` and cleared
    ``[t.to_pylist() for t in tables]`` — even though the parameter ``tables``
    shadows the global at runtime and holds whatever the caller passed (an SDK
    reader frame, say). Parameters are now recorded as unknown/non-pyarrow
    bindings in the function's scope, so the shadowing name voids the
    exemption before the walk ever reaches the module binding.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "tables = [pa.table({}) for _ in range(3)]\n"
        + "\n"
        + "def f(tables):\n"
        + "    return [t.to_pylist() for t in tables]\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_lambda_parameter_shadowing_a_module_pyarrow_binding_does_not_exempt() -> (
    None
):
    """A same-named lambda parameter kills an enclosing pyarrow exemption.

    ``ast.Lambda`` was absent from both ``_FUNCTION_SCOPES`` and
    ``_SCOPE_NODES``, so a lambda parameter created no binding and the outward
    walk reached the module-level ``tables = [pa.table({}) ...]`` — clearing
    ``[t.to_pylist() for t in tables]`` even though the lambda's ``tables``
    shadows the global exactly as a ``def`` parameter does. Lambdas now open a
    scope and record their parameters as unknown/non-pyarrow.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "tables = [pa.table({}) for _ in range(3)]\n"
        + "\n"
        + "f = lambda tables: [t.to_pylist() for t in tables]\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_rebinding_a_parameter_to_pyarrow_restores_the_exemption() -> None:
    """A parameter rebound to pyarrow inside the body is exempt from that line.

    Parameters are recorded as unknown/non-pyarrow at the ``def`` line, which
    sorts before every use — so a later ``tables = [pa.table({}) ...]`` inside
    the body wins the own-scope last-binding-before-the-use rule, and the
    shadow-then-rebind sequence ends pyarrow, exactly as the runtime does.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f(tables):\n"
        + "    tables = [pa.table({}) for _ in range(3)]\n"
        + "    return [t.to_pylist() for t in tables]\n"
    )
    assert _b007(src) == []


def test_b007_pyarrow_iterable_in_an_enclosing_scope_still_exempts() -> None:
    """The enclosing chain is still honoured — only sibling scopes are cut off."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def outer():\n"
        + "    tables = [pa.table({}) for _ in range(3)]\n"
        + "\n"
        + "    def inner():\n"
        + "        return [t.to_pylist() for t in tables]\n"
        + "\n"
        + "    return inner\n"
    )
    assert _b007(src) == []


def test_b007_loop_over_a_pyarrow_iterable_is_exempt() -> None:
    """`for t in [pa.table({})]` binds a real Table — same shape as a comprehension.

    Hardcoding the loop branch to "never a producer call" contradicted the
    comprehension branch ten lines below, which inspects the iterable and is
    right. One helper now serves both.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f():\n"
        + "    for t in [pa.table({})]:\n"
        + "        return t.to_pylist()\n"
    )
    assert _b007(src) == []


def test_b007_loop_over_reader_frames_still_fires() -> None:
    src = (
        _SDK_IMPORT
        + "def f(pages):\n"
        + "    for t in pages:\n"
        + "        return t.to_pylist()\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_chained_assignment_kills_a_stale_exemption() -> None:
    """`a = df = frame` — the `len(targets) == 1` guard dropped the rebinding."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f(frame):\n"
        + "    df = pa.table({})\n"
        + "    a = df = frame\n"
        + "    return df.to_pylist()\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_tuple_unpacking_kills_a_stale_exemption() -> None:
    """`df, other = frame, 1` — `target.id` silently dropped unpacking targets."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f(frame):\n"
        + "    df = pa.table({})\n"
        + "    df, other = frame, 1\n"
        + "    return df.to_pylist()\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_augmented_assignment_kills_a_stale_exemption() -> None:
    """`df += frame` rebinds `df` to an unknown value — the exemption must die.

    `ast.AugAssign` was the one binding form the walk never recorded, so a
    pyarrow-bound `df` kept its exemption past the `+=` and the genuine SDK
    reader frame on the next line went unreported.
    """
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f(frame):\n"
        + "    df = pa.table({})\n"
        + "    df += frame\n"
        + "    return df.to_pylist()\n"
    )
    assert [f.rule_id for f in _b007(src)] == ["B007"]


def test_b007_tuple_unpacking_pairs_element_wise() -> None:
    """`df, other = pa.table({}), 1` binds a genuine Table to `df`."""
    src = (
        _SDK_IMPORT
        + "import pyarrow as pa\n"
        + "\n"
        + "def f():\n"
        + "    df, other = pa.table({}), 1\n"
        + "    return df.to_pylist()\n"
    )
    assert _b007(src) == []
