"""Meta-tests for the B-series deprecation checks (B001–B004).

These checks fan out across the fleet — a buggy check false-positives across many
apps and triggers spurious remediations.  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false negatives
are guarded.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _parse_directives
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
from conformance.suite.checks.deprecation._version import parse_version, version_reached


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
