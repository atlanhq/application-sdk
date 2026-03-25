"""Parse mutatedDetails from Kafka events — shared by ALL source apps."""

from typing import Any

from application_sdk.common.reverse_sync.models import SourceTagInfo


def _extract_tag_value(source_tag_value: list[dict[str, Any]]) -> str:
    """Extract tag value (handles both nesting formats)."""
    if not source_tag_value:
        return ""
    first = source_tag_value[0]
    if "attributes" in first:
        return first["attributes"].get("tagAttachmentValue", "")
    return first.get("tagAttachmentValue", "")


def _extract_from_attachment(
    item: dict[str, Any], classification_type_name: str, entity_guid: str
) -> SourceTagInfo | None:
    """Extract SourceTagInfo from a single attachment item."""
    # Nested: {typeName: "SourceTagAttachment", attributes: {sourceTagName: ...}}
    if "attributes" in item and "sourceTagName" in item.get("attributes", {}):
        a = item["attributes"]
        return SourceTagInfo(
            source_tag_name=a.get("sourceTagName", ""),
            source_tag_qualified_name=a.get("sourceTagQualifiedName", ""),
            source_tag_guid=a.get("sourceTagGuid", ""),
            source_tag_connector_name=a.get("sourceTagConnectorName", ""),
            tag_value=_extract_tag_value(a.get("sourceTagValue", [])),
            classification_type_name=classification_type_name,
            entity_guid=entity_guid,
        )
    # Flat: {sourceTagName: ..., sourceTagValue: [...]}
    if "sourceTagName" in item:
        return SourceTagInfo(
            source_tag_name=item.get("sourceTagName", ""),
            source_tag_qualified_name=item.get("sourceTagQualifiedName", ""),
            source_tag_guid=item.get("sourceTagGuid", ""),
            source_tag_connector_name=item.get("sourceTagConnectorName", ""),
            tag_value=_extract_tag_value(item.get("sourceTagValue", [])),
            classification_type_name=classification_type_name,
            entity_guid=entity_guid,
        )
    return None


def extract_source_tags(
    mutated_details: list[dict[str, Any]],
    connector_filter: str | None = None,
) -> list[SourceTagInfo]:
    """Extract all source tag attachments from mutatedDetails.

    Works identically for Snowflake, Databricks, BigQuery — the Kafka event
    format is the same regardless of source connector.

    Args:
        mutated_details: The mutatedDetails array from Kafka event.
        connector_filter: If set, only include tags matching this connector.
    """
    results: list[SourceTagInfo] = []
    for md in mutated_details:
        cls_type_name = md.get("typeName", "")
        entity_guid = md.get("entityGuid", "")
        attrs = md.get("attributes", {})
        for _attr_key, attachments in attrs.items():
            if not isinstance(attachments, list):
                continue
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                tag_info = _extract_from_attachment(item, cls_type_name, entity_guid)
                if tag_info is None:
                    continue
                if (
                    connector_filter
                    and tag_info.source_tag_connector_name != connector_filter
                ):
                    continue
                results.append(tag_info)
    return results
