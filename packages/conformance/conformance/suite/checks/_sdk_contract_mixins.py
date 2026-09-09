"""Static field registries for SDK-provided contract classes.

``resolve_contract_fields`` in the neutral ``_entrypoint_contract_fields``
module resolves contract fields across the full inheritance hierarchy — used
today by the B005/B006 checker and the ledger generator, and expected to also
back the K-series. In-repo base classes are resolved directly from source via
the cross-file class registry (``collect_classes`` / ``by_name``). SDK-provided
contract classes are *not* part of the scanned repo when a checker runs against
a consumer app, so their fields are mirrored here instead, in two dicts:

- :data:`SDK_CONTRACT_BASE_FIELDS` — the bases and mixins in
  ``application_sdk.contracts.base`` (``Input`` / ``Output`` /
  ``PublishInputMixin``), each carrying only its own declared fields.
- :data:`SDK_TEMPLATE_CONTRACT_FIELDS` — the template contracts in
  ``application_sdk.templates.contracts``, each carrying its fields *flattened*
  across its own base chain.

Keep both in sync with the SDK. ``tests/test_sdk_contract_mixins.py`` guards
drift by rebuilding each registry from a live AST scan of the SDK source when
the ``atlan-application-sdk`` test extra is installed.
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
        SdkField("workflow_slug", "str", "active"),
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

# ── SDK template contracts ────────────────────────────────────────────────────
# Template contracts (``application_sdk.templates.contracts.*``) are imported
# from the installed SDK and are therefore absent from a consumer app's AST
# scan, so a consumer contract that subclasses one resolves to no inherited
# fields at all — every ledger entry for those fields reads as removed (B005)
# or unrecorded (B006). They are mirrored here for the same reason as
# ``SDK_CONTRACT_BASE_FIELDS`` above, but kept in a separate dict so the
# base/mixin drift test can keep targeting ``contracts.base`` alone.
#
# Scope: every contract class declared under
# ``application_sdk/templates/contracts/`` — a mechanical boundary rather than
# a curated shortlist, because ``resolve_contract_fields`` returns no fields
# for *any* template name it does not find here, so a partial catalog just
# defers the same false positive to the next class a consumer subclasses.
#
# Entries are FLATTENED: each tuple carries the class's own fields plus every
# field it inherits, including from other template contracts. Flattening is
# required, not an optimisation — the lookup is by the single unresolved base
# name and does not recurse, so ``IncrementalExtractionOutput`` cannot reach
# ``ExtractionOutput``'s fields through its own registry entry.
#
# Excluded: bare names that another SDK contract module also declares
# (``UploadInput`` / ``UploadOutput``, declared by both
# ``templates.contracts.base_metadata_extraction`` and ``contracts.storage``
# with different fields). This registry is keyed by bare class name, matching
# how the resolver sees an unresolved base, so it cannot tell the two apart;
# registering either one's fields would mis-resolve consumers of the other.
# Leaving them out preserves the conservative status quo — no fields resolved,
# which is what happens today for every unregistered name.
#
# Generated from the ``atlan-application-sdk`` version pinned by this
# package's ``test`` extra. ``tests/test_sdk_contract_mixins.py`` reproduces
# the flattening against the live template modules and fails on any drift,
# any newly added template contract, and any change to the collision set.
SDK_TEMPLATE_CONTRACT_FIELDS: dict[str, tuple[SdkField, ...]] = {
    "ExecuteColumnBatchInput": (
        SdkField("app_name", "str", "active"),
        SdkField("application_name", "str", "active"),
        SdkField("batch_index", "int", "active"),
        SdkField("batches_s3_prefix", "str", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("total_batches", "int", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "ExecuteColumnBatchOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("batch_index", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("records", "int", "active"),
        SdkField("status", "str", "active"),
    ),
    "ExtractionInput": (
        SdkField("agent_json", "AgentCredentialSpec | None", "active"),
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("extraction_method", "str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "ExtractionOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("columns_extracted", "int", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("current_state_prefix", "str", "active"),
        SdkField("databases_extracted", "int", "active"),
        SdkField("error", "str", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("procedures_extracted", "int", "active"),
        SdkField("processes_extracted", "int", "active"),
        SdkField("publish_state_prefix", "str", "active"),
        SdkField("records_uploaded", "int", "active"),
        SdkField("schemas_extracted", "int", "active"),
        SdkField("staging_data_prefix", "str", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("success", "bool", "active"),
        SdkField("tables_extracted", "int", "active"),
        SdkField("transformed_data_prefix", "str", "active"),
        SdkField("views_extracted", "int", "active"),
        SdkField("workflow_id", "str", "active"),
    ),
    "ExtractionTaskInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "ExtractionTaskOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("raw_file", "FileReference | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
        SdkField("typename", "str", "active"),
    ),
    "FetchColumnsIncrementalInput": (
        SdkField("app_name", "str", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchColumnsInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchColumnsOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
    ),
    "FetchDatabasesInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchDatabasesOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("databases", "list[str]", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
    ),
    "FetchIncrementalMarkerInput": (
        SdkField("app_name", "str", "active"),
        SdkField("application_name", "str", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("existing_marker", "str | None", "active"),
        SdkField("prepone_enabled", "bool", "active"),
        SdkField("prepone_hours", "float", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchIncrementalMarkerOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("next_marker_timestamp", "str", "active"),
        SdkField("status", "OutputStatus", "active"),
    ),
    "FetchProceduresInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchProceduresOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
    ),
    "FetchSchemasInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchSchemasOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("schemas", "list[str]", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
    ),
    "FetchTablesIncrementalInput": (
        SdkField("app_name", "str", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchTablesInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchTablesOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("tables", "list[str]", "active"),
        SdkField("total_record_count", "int", "active"),
    ),
    "FetchViewsInput": (
        SdkField("app_name", "str", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "FetchViewsOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
    ),
    "IncrementalExtractionInput": (
        SdkField("agent_json", "AgentCredentialSpec | None", "active"),
        SdkField("app_name", "str", "active"),
        SdkField("column_batch_size", "int", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("copy_workers", "int", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("extraction_method", "str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("prepone_marker_hours", "int", "active"),
        SdkField("prepone_marker_timestamp", "bool", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("upload_concurrency", "int", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "IncrementalExtractionOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("backfill_tables", "int", "active"),
        SdkField("changed_tables", "int", "active"),
        SdkField("column_batches_executed", "int", "active"),
        SdkField("columns_extracted", "int", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("current_state_files", "int", "active"),
        SdkField("current_state_prefix", "str", "active"),
        SdkField("databases_extracted", "int", "active"),
        SdkField("error", "str", "active"),
        SdkField("incremental_diff_files", "int", "active"),
        SdkField("marker_updated", "bool", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("procedures_extracted", "int", "active"),
        SdkField("processes_extracted", "int", "active"),
        SdkField("publish_state_prefix", "str", "active"),
        SdkField("records_uploaded", "int", "active"),
        SdkField("schemas_extracted", "int", "active"),
        SdkField("staging_data_prefix", "str", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("success", "bool", "active"),
        SdkField("tables_extracted", "int", "active"),
        SdkField("transformed_data_prefix", "str", "active"),
        SdkField("views_extracted", "int", "active"),
        SdkField("workflow_id", "str", "active"),
    ),
    "IncrementalTaskInput": (
        SdkField("app_name", "str", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "PrepareColumnQueriesInput": (
        SdkField("app_name", "str", "active"),
        SdkField("application_name", "str", "active"),
        SdkField("column_batch_size", "int", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("current_state_s3_prefix", "str", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "PrepareColumnQueriesOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("backfill_tables", "int", "active"),
        SdkField("batches_local_dir", "str", "active"),
        SdkField("batches_s3_prefix", "str", "active"),
        SdkField("changed_tables", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_batches", "int", "active"),
        SdkField("total_tables", "int", "active"),
    ),
    "PrimeAuthOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("duration_ms", "float", "active"),
        SdkField("error_message", "str | None", "active"),
        SdkField("error_type", "str | None", "active"),
        SdkField("failure", "FailureDetails | None", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("success", "bool", "active"),
    ),
    "QueryBatchInput": (
        SdkField("app_name", "str", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField(
            "workflow_args", "dict[str, str | int | float | bool | None]", "active"
        ),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "QueryBatchOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("batch_size", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_batches", "int", "active"),
        SdkField("total_count", "int", "active"),
    ),
    "QueryExtractionInput": (
        SdkField("app_name", "str", "active"),
        SdkField("batch_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_users", "list[str]", "active"),
        SdkField("lookback_days", "int", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "QueryExtractionOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("error", "str", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("records_uploaded", "int", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("success", "bool", "active"),
        SdkField("total_batches", "int", "active"),
        SdkField("total_queries", "int", "active"),
        SdkField("workflow_id", "str", "active"),
    ),
    "QueryFetchInput": (
        SdkField("app_name", "str", "active"),
        SdkField("batch_number", "int", "active"),
        SdkField("batch_size", "int", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField(
            "workflow_args", "dict[str, str | int | float | bool | None]", "active"
        ),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "QueryFetchOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("batch_number", "int", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("queries_fetched", "int", "active"),
        SdkField("status", "OutputStatus", "active"),
    ),
    "ReadCurrentStateInput": (
        SdkField("app_name", "str", "active"),
        SdkField("application_name", "str", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "ReadCurrentStateOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("current_state_json_count", "int", "active"),
        SdkField("current_state_path", "str", "active"),
        SdkField("current_state_s3_prefix", "str", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
    ),
    "TransformInput": (
        SdkField("app_name", "str", "active"),
        SdkField("chunk_start", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("file_names", "list[str]", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("raw_file", "FileReference | None", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("typename", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "TransformOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("chunk_count", "int", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
        SdkField("total_record_count", "int", "active"),
        SdkField("transformed_file", "FileReference | None", "active"),
        SdkField("typename", "str", "active"),
    ),
    "UpdateMarkerInput": (
        SdkField("app_name", "str", "active"),
        SdkField("application_name", "str", "active"),
        SdkField("connection_qualified_name", "str", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("next_marker_timestamp", "str", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
    ),
    "UpdateMarkerOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("marker_written", "bool", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("s3_key", "str", "active"),
        SdkField("status", "OutputStatus", "active"),
    ),
    "WriteCurrentStateInput": (
        SdkField("app_name", "str", "active"),
        SdkField("application_name", "str", "active"),
        SdkField("column_chunk_size", "int", "active"),
        SdkField("connection", "ConnectionRef", "active"),
        SdkField("copy_workers", "int", "active"),
        SdkField("correlation_id", "str", "active"),
        SdkField("credential_guid", "str", "active"),
        SdkField("credential_ref", "CredentialRef | None", "active"),
        SdkField("current_state_available", "bool", "active"),
        SdkField("current_state_s3_prefix", "str", "active"),
        SdkField("exclude_filter", "FilterMap | str", "active"),
        SdkField("include_filter", "FilterMap | str", "active"),
        SdkField("incremental_extraction", "bool", "active"),
        SdkField("marker_timestamp", "str", "active"),
        SdkField("output_path", "str", "active"),
        SdkField("output_prefix", "str", "active"),
        SdkField("source_tag_prefix", "str", "active"),
        SdkField("temp_table_regex", "str", "active"),
        SdkField("upload_concurrency", "int", "active"),
        SdkField("workflow_id", "str", "active"),
        SdkField("workflow_slug", "str", "active"),
        SdkField("workflow_run_id", "str", "active"),
    ),
    "WriteCurrentStateOutput": (
        SdkField("artifacts", "dict[str, Any] | None", "active"),
        SdkField("current_state_files", "int", "active"),
        SdkField("current_state_path", "str", "active"),
        SdkField("current_state_s3_prefix", "str", "active"),
        SdkField("incremental_diff_files", "int", "active"),
        SdkField("incremental_diff_path", "str", "active"),
        SdkField("incremental_diff_s3_prefix", "str", "active"),
        SdkField("metrics", "dict[str, Any] | None", "active"),
        SdkField("status", "OutputStatus", "active"),
    ),
}

# ── Model-declared artifact fields ────────────────────────────────────────────
# A ``FileReference`` field can carry the SDK's ``AssetArtifact`` marker
# (``application_sdk.contracts.types``), which says its declaration *is* an
# executable model — ``pyatlan_v9``'s ``Asset`` — rather than a hand-authored
# field map in ``artifactSchemas``. The SDK's registration-time guard exempts
# such a field, and its activity interceptor validates it against the whole
# model instead, so K016 must exempt it too: a rule demanding a declaration the
# SDK does not want, for an artifact it already checks more strictly, would be
# asking every app to author a partial restatement of ``Asset`` (FND-1863).
#
# Names, not a per-class map, and that is a deliberate accuracy trade. The
# marker lives in ``Annotated`` metadata, which ``_canonical_type`` strips, so a
# field *inherited* from an SDK contract arrives at the check as a bare name with
# no annotation left to inspect — the same reason ``SDK_TEMPLATE_CONTRACT_FIELDS``
# exists at all. A field an app declares itself is matched on its own annotation
# instead (see ``artifact_schema_declared._check``), so the only case this set
# answers is the inherited one, where the app has nothing of its own to mark.
#
# The residual false negative is an app declaring an unrelated boundary field
# that happens to share one of these names *and* inheriting it from a base this
# scan cannot see. K016 is a WARN-tier rule that errs toward a false negative by
# design, and this gap is narrower than the import-alias one it already accepts.
#
# ``tests/test_sdk_contract_mixins.py`` rebuilds this set from an AST scan of the
# installed SDK's contract modules, so a newly marked field cannot ship without
# the rule learning about it, and a name the SDK has stopped marking cannot sit
# here exempting fields forever — see the ahead-of-pin allowlist below for the one
# window in which an entry may have no live marker backing it.
SDK_MODEL_BACKED_ARTIFACT_FIELDS: frozenset[str] = frozenset({"transformed_files"})

#: Entries of :data:`SDK_MODEL_BACKED_ARTIFACT_FIELDS` the *pinned* SDK does not
#: mark yet, because the marker landed in the SDK after this package's pin.
#:
#: The two drift directions need different rules, and this is what lets both be
#: checked. A name the SDK marks and the mirror omits is a fleet-wide false
#: positive, so that direction is absolute. A mirror entry with no live marker is
#: either (a) this window — the marker is on the SDK's ``main`` and the pin has
#: not caught up — or (b) genuine staleness, where a removed or renamed marker
#: leaves a name behind that goes on exempting every inherited field sharing it,
#: silently and with the test still green. Only (a) is legitimate, and only (a) is
#: listed here.
#:
#: **Every entry is a deletion waiting for a pin bump.** Once
#: ``packages/conformance/uv.lock`` moves to an SDK that carries the marker, the
#: drift test sees it live and fails until the name is removed from this
#: allowlist — which is the point: the exemption stays, the temporary excuse for
#: it does not.
MODEL_BACKED_FIELDS_AHEAD_OF_PIN: frozenset[str] = frozenset({"transformed_files"})
