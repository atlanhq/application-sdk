"""Guards on the SDK contract-field registries that a table rebuild cannot give.

The four generated registries (``SDK_CONTRACT_BASE_FIELDS``,
``SDK_TEMPLATE_CONTRACT_FIELDS``, ``SDK_TEMPLATE_CONTRACT_BASES``,
``SDK_TEMPLATE_RUN_CONTRACTS``) are checked for drift by
``tests/test_sdk_contract_fields_drift.py``, which runs
``gen-sdk-contract-fields --check``. This module keeps what that check cannot
see: the collision set the generator excludes, runtime facts about the
templates K018 relies on, cross-registry consistency, and the hand-curated
model-backed artifact registry.
"""

from __future__ import annotations

import ast
import functools

import pytest

from conformance.suite.checks._ast_common._sdk_app import SDK_APP_BASE_NAMES
from conformance.suite.checks._entrypoint_contract_fields import _ASSET_ARTIFACT_RE
from conformance.suite.checks._sdk_contract_mixins import (
    MODEL_BACKED_FIELDS_AHEAD_OF_PIN,
    SDK_CONTRACT_BASE_FIELDS,
    SDK_MODEL_BACKED_ARTIFACT_FIELDS,
    SDK_TEMPLATE_CONTRACT_FIELDS,
    SDK_TEMPLATE_RUN_CONTRACTS,
    SdkField,
)
from conformance.tools.generate_sdk_contract_fields import (
    SdkSources,
    ambiguous_template_names,
    find_sdk_package,
    parse_sdk_sources,
)

_requires_sdk = pytest.mark.skipif(
    find_sdk_package() is None,
    reason="atlan-application-sdk (test extra) is not installed",
)


@functools.cache
def _sdk_sources() -> SdkSources:
    sdk_pkg = find_sdk_package()
    assert sdk_pkg is not None
    return parse_sdk_sources(sdk_pkg)


def test_registry_fields_are_well_formed() -> None:
    """Every registry entry is a valid SdkField with a non-empty name/type."""
    for class_name, entries in SDK_CONTRACT_BASE_FIELDS.items():
        assert entries, f"{class_name} has an empty field list"
        for entry in entries:
            assert isinstance(entry, SdkField)
            assert entry.name
            assert entry.canonical_type
            assert entry.status in ("active", "deprecated", "sunset")


@_requires_sdk
def test_template_registry_excludes_only_ambiguous_bare_names() -> None:
    """The excluded set is exactly the bare names the SDK declares twice.

    The registry is keyed by bare class name, so a name two SDK contract
    modules declare cannot be resolved to one of them; the generator leaves
    those out on purpose. Pinning the set here means a new collision — which
    silently drops a template contract from the registry — or a removed one
    surfaces as a failing test rather than as a permanent silent gap.
    """
    ambiguous = ambiguous_template_names(_sdk_sources())
    assert sorted(ambiguous) == ["UploadInput", "UploadOutput"], (
        "the set of ambiguous template contract bare names changed — "
        f"now { ({k: sorted(v) for k, v in ambiguous.items()}) }. Extend the "
        "exclusion note in _sdk_contract_mixins.py for any new one."
    )
    for name in ambiguous:
        assert name not in SDK_TEMPLATE_CONTRACT_FIELDS, (
            f"{name} is declared by more than one SDK contract module "
            f"({sorted(ambiguous[name])}) — a bare-name-keyed registry cannot "
            "tell them apart, so it must not be registered"
        )


def test_template_registry_fields_are_well_formed() -> None:
    """Every template registry entry is a valid, non-empty SdkField tuple."""
    for class_name, entries in SDK_TEMPLATE_CONTRACT_FIELDS.items():
        assert entries, f"{class_name} has an empty field list"
        for entry in entries:
            assert isinstance(entry, SdkField)
            assert entry.name
            assert entry.canonical_type
            assert entry.status in ("active", "deprecated", "sunset")


# -- Template run() contracts --------------------------------------------------


@_requires_sdk
def test_template_names_match_sdk_app_base_names() -> None:
    """Every SDK template is an app base name the K-series recognises."""
    import application_sdk.templates as templates

    assert set(templates.__all__) | {"App"} == SDK_APP_BASE_NAMES


@_requires_sdk
def test_template_run_inputs_do_not_keep_undeclared_keys() -> None:
    """K018 reads the table's fields only, so no template Input may set extra="allow"."""
    import typing

    import application_sdk.templates as templates

    for name in SDK_TEMPLATE_RUN_CONTRACTS:
        input_type = typing.get_type_hints(getattr(templates, name).run)["input"]
        assert input_type.model_config.get("extra") != "allow", name


def test_template_run_registry_inputs_have_registered_fields() -> None:
    for name, contract in SDK_TEMPLATE_RUN_CONTRACTS.items():
        assert name in SDK_APP_BASE_NAMES
        assert contract.input in SDK_TEMPLATE_CONTRACT_FIELDS, name
        assert contract.output in SDK_TEMPLATE_CONTRACT_FIELDS, name


# -- Model-declared artifact fields -------------------------------------------


def _live_model_backed_artifact_fields() -> dict[str, set[str]]:
    """Every SDK contract field carrying the ``AssetArtifact`` marker.

    Read off the raw annotation AST, which is the only place the marker exists:
    it lives in ``Annotated`` metadata, and ``_canonical_type`` — the form the
    checker sees for an inherited field — strips exactly that.  Restricted to
    contract classes, because a marked field only reaches K016 as an entrypoint
    boundary field.

    Returns:
        ``{field name: {owning class, ...}}``, so a failure can name the class
        whose new marker has not been mirrored.
    """
    sources = _sdk_sources()
    marked: dict[str, set[str]] = {}
    for _module, (_aliases, records) in sources.per_module.items():
        for record in records:
            if record.name not in sources.contract_owners:
                continue
            for stmt in record.node.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                    stmt.target, ast.Name
                ):
                    continue
                if _ASSET_ARTIFACT_RE.search(ast.unparse(stmt.annotation)):
                    marked.setdefault(stmt.target.id, set()).add(record.name)
    return marked


@_requires_sdk
def test_model_backed_registry_covers_every_live_marker() -> None:
    """Every field the installed SDK marks is mirrored in the static set.

    **Subset, not equality, and the asymmetry is the pin.**  This package's test
    leg installs the *published* SDK from its own ``uv.lock``, not the SDK
    sitting next to it in this repo, so the registry legitimately runs ahead of
    the installed marker set for as long as the pin is behind — that window is
    the normal state after an SDK change and before the bump.  Failing on it
    would make every such window red without naming a real defect.

    The direction that is checked is the one that costs the fleet: a field the
    SDK marks and this set omits is a K016 finding demanding a declaration the
    SDK does not want, on every app that inherits the field.  A stale entry —
    a name the SDK has stopped marking — is the residual gap, and it costs one
    unasked-about boundary rather than a fleet-wide false positive.
    """
    live = _live_model_backed_artifact_fields()
    unmirrored = {
        name: sorted(owners)
        for name, owners in live.items()
        if name not in SDK_MODEL_BACKED_ARTIFACT_FIELDS
    }
    assert not unmirrored, (
        "the installed SDK marks contract field(s) with AssetArtifact that "
        "SDK_MODEL_BACKED_ARTIFACT_FIELDS does not list, so K016 will demand an "
        "artifactSchemas entry the SDK exempts — add them in "
        f"_sdk_contract_mixins.py: {unmirrored}"
    )


@_requires_sdk
def test_model_backed_registry_has_no_stale_entries() -> None:
    """No mirror entry exempts a field the pinned SDK does not mark.

    The other direction, and the one a subset assertion cannot see. A marker the
    SDK later removes or renames leaves its name behind here, where it goes on
    exempting every inherited field that happens to share it — silently, on a
    boundary nothing then model-validates, with the drift test still green.

    The pin-lag window is the one legitimate reason for an entry with no live
    marker, and it is named explicitly in ``MODEL_BACKED_FIELDS_AHEAD_OF_PIN``
    rather than blanket-tolerated. When the pin catches up, the name shows up
    live, this test's allowlist expectation stops holding, and the entry has to be
    removed from the allowlist — see :func:`test_ahead_of_pin_allowlist_is_current`.
    """
    live = set(_live_model_backed_artifact_fields())
    stale = sorted(
        SDK_MODEL_BACKED_ARTIFACT_FIELDS - live - MODEL_BACKED_FIELDS_AHEAD_OF_PIN
    )
    assert not stale, (
        "SDK_MODEL_BACKED_ARTIFACT_FIELDS exempts field name(s) the installed SDK "
        "does not mark with AssetArtifact, so K016 is waving through boundary "
        f"fields nothing declares: {stale}. Remove them from "
        "_sdk_contract_mixins.py, or — if the marker is on the SDK's main and "
        "this package's pin has not caught up — add them to "
        "MODEL_BACKED_FIELDS_AHEAD_OF_PIN with that reason."
    )


@_requires_sdk
def test_ahead_of_pin_allowlist_is_current() -> None:
    """The allowlist holds only names the pinned SDK genuinely does not mark yet.

    This is what stops the pin-lag excuse outliving the pin lag. Once the pin
    moves to an SDK carrying the marker, the name is live, the allowlist entry is
    no longer excusing anything, and it has to go — otherwise the next genuinely
    stale entry hides behind it.
    """
    live = set(_live_model_backed_artifact_fields())
    landed = sorted(MODEL_BACKED_FIELDS_AHEAD_OF_PIN & live)
    assert not landed, (
        "MODEL_BACKED_FIELDS_AHEAD_OF_PIN lists field name(s) the pinned SDK now "
        f"marks, so the pin has caught up: {landed}. Remove them from the "
        "allowlist in _sdk_contract_mixins.py — the exemption stays, only the "
        "temporary excuse for it goes."
    )


def test_ahead_of_pin_allowlist_is_a_subset_of_the_registry() -> None:
    """An allowlist entry that is not a registry entry excuses nothing."""
    orphans = sorted(
        MODEL_BACKED_FIELDS_AHEAD_OF_PIN - SDK_MODEL_BACKED_ARTIFACT_FIELDS
    )
    assert not orphans, (
        f"MODEL_BACKED_FIELDS_AHEAD_OF_PIN names {orphans}, which "
        "SDK_MODEL_BACKED_ARTIFACT_FIELDS does not exempt — drop them, or add the "
        "matching registry entries they were meant to excuse."
    )


def test_model_backed_registry_is_well_formed() -> None:
    """Every entry is a non-empty field name."""
    assert SDK_MODEL_BACKED_ARTIFACT_FIELDS
    for name in SDK_MODEL_BACKED_ARTIFACT_FIELDS:
        assert name and isinstance(name, str)
