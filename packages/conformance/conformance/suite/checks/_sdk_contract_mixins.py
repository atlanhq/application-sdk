"""Static field registry for SDK-provided contract base classes and mixins.

``resolve_contract_fields`` in the neutral ``_entrypoint_contract_fields``
module resolves contract fields across the full inheritance hierarchy — used
today by the B005/B006 checker and the ledger generator, and expected to also
back the K-series. In-repo base classes are resolved directly from source via
the cross-file class registry (``collect_classes`` / ``by_name``). SDK-provided
contract bases (``application_sdk.contracts.base.Input`` / ``Output`` /
``PublishInputMixin``) are *not* part of the scanned repo when a checker runs
against a consumer app, so their fields are hand-mirrored here instead.

Keep this in sync with ``application_sdk/contracts/base.py``.
``tests/test_sdk_contract_mixins.py`` guards drift by comparing this registry
against a live AST scan of the SDK source when the ``atlan-application-sdk``
test extra is installed.
"""

from __future__ import annotations

from typing import NamedTuple


class SdkField(NamedTuple):
    """One statically-recorded field on an SDK contract base class."""

    name: str
    canonical_type: str
    status: str  # "active" | "deprecated" | "sunset"


# Mirrors application_sdk/contracts/base.py. Only fields visible on instances
# (Pydantic model fields) are listed here — ClassVar sentinels, methods, and
# validators are excluded, matching what `_iter_fields` would extract from
# source for these same classes.
SDK_CONTRACT_BASE_FIELDS: dict[str, tuple[SdkField, ...]] = {
    "Input": (
        SdkField("workflow_id", "str", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("app_name", "str", "active"),
    ),
    "Output": (
        SdkField("status", "OutputStatus", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("artifacts", "dict[str, Any] | None", "active"),
    ),
    "PublishInputMixin": (
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("transformed_data_prefix", "str", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("publish_state_prefix", "str", "active"),
        SdkField("staging_data_prefix", "str", "active"),
        SdkField("current_state_prefix", "str", "active"),
    ),
}

# Template contracts are imported from the installed SDK and therefore are not
# present in a consumer app's AST scan. Keep their declared fields separately
# from the base/mixin registry, whose drift test targets ``contracts.base``.
SDK_TEMPLATE_CONTRACT_FIELDS: dict[str, tuple[SdkField, ...]] = {
    "ExtractionOutput": (
        SdkField("status", "OutputStatus", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("transformed_data_prefix", "str", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("publish_state_prefix", "str", "active"),
        SdkField("staging_data_prefix", "str", "active"),
        SdkField("current_state_prefix", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("success", "bool", "active"),
        SdkField("databases_extracted", "int", "active"),
        SdkField("schemas_extracted", "int", "active"),
        SdkField("tables_extracted", "int", "active"),
        SdkField("views_extracted", "int", "active"),
        SdkField("columns_extracted", "int", "active"),
        SdkField("procedures_extracted", "int", "active"),
        SdkField("processes_extracted", "int", "active"),
        SdkField("records_uploaded", "int", "active"),
        SdkField("error", "str", "active"),
        SdkField("output_path", "str", "active"),
    ),
}
