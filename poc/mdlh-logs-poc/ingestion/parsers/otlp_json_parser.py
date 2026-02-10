"""
OTLP JSON log parser - parses logs from OTEL collector's awss3 exporter.

The awss3 exporter outputs OTLP JSON format with structure:
{
    "resourceLogs": [{
        "resource": {
            "attributes": [{"key": "service.name", "value": {"stringValue": "..."}}]
        },
        "scopeLogs": [{
            "logRecords": [{
                "timeUnixNano": "...",
                "severityText": "INFO",
                "body": {"stringValue": "..."},
                "attributes": [{"key": "workflow_id", "value": {"stringValue": "..."}}]
            }]
        }]
    }]
}
"""

import json
from datetime import datetime
from typing import Dict, Any, List, Optional

import pyarrow as pa

from .base import LogParser


class OTLPJsonParser(LogParser):
    """Parser for OTLP JSON files from awss3 exporter."""

    @property
    def name(self) -> str:
        return "otlp_json"

    def can_parse(self, filename: str) -> bool:
        """Check if file is a JSON file (OTLP JSON format)."""
        return filename.lower().endswith('.json')

    def parse(self, file_content: bytes, arrow_schema: pa.Schema) -> pa.Table:
        """Parse OTLP JSON content and normalize to target schema."""
        data = json.loads(file_content.decode('utf-8'))
        records = self._extract_records(data)

        if not records:
            # Return empty table with correct schema
            return pa.Table.from_pylist([], schema=arrow_schema)

        return pa.Table.from_pylist(records, schema=arrow_schema)

    def _extract_attrs(self, attrs_list: List[Dict]) -> Dict[str, Any]:
        """
        Convert OTLP attributes list to dict.

        OTLP format: [{"key": "name", "value": {"stringValue": "..."}}]
        """
        result = {}
        if not attrs_list:
            return result

        for attr in attrs_list:
            key = attr.get("key")
            if not key:
                continue

            value = attr.get("value", {})
            # Handle different OTLP value types
            if "stringValue" in value:
                result[key] = value["stringValue"]
            elif "intValue" in value:
                result[key] = int(value["intValue"])
            elif "doubleValue" in value:
                result[key] = float(value["doubleValue"])
            elif "boolValue" in value:
                result[key] = value["boolValue"]
            elif "arrayValue" in value:
                # Handle array values (convert to string for now)
                result[key] = str(value["arrayValue"])
            elif "kvlistValue" in value:
                # Handle nested key-value lists
                result[key] = str(value["kvlistValue"])

        return result

    def _extract_month(self, time_unix_nano: str) -> int:
        """Extract month number from Unix nano timestamp."""
        try:
            timestamp_sec = int(time_unix_nano) / 1e9
            dt = datetime.utcfromtimestamp(timestamp_sec)
            return dt.month
        except (ValueError, TypeError, OSError):
            return datetime.utcnow().month

    def _extract_records(self, data: Dict) -> List[Dict]:
        """
        Extract log records from OTLP JSON structure.

        Maps OTLP fields to the Iceberg schema fields.
        """
        records = []

        for resource_log in data.get("resourceLogs", []):
            # Extract resource attributes (service.name, etc.)
            resource_attrs = self._extract_attrs(
                resource_log.get("resource", {}).get("attributes", [])
            )

            for scope_log in resource_log.get("scopeLogs", []):
                # Get scope info if available
                scope = scope_log.get("scope", {})
                scope_name = scope.get("name", "")

                for log_record in scope_log.get("logRecords", []):
                    # Extract log record attributes
                    log_attrs = self._extract_attrs(
                        log_record.get("attributes", [])
                    )

                    # Get timestamp
                    time_unix_nano = log_record.get("timeUnixNano", "0")
                    try:
                        timestamp = int(time_unix_nano) / 1e9
                    except (ValueError, TypeError):
                        timestamp = datetime.utcnow().timestamp()

                    # Get body (message)
                    body = log_record.get("body", {})
                    if isinstance(body, dict):
                        message = body.get("stringValue", "")
                    else:
                        message = str(body)

                    # app_name: prefer log attrs, fallback to resource service.name
                    app_name = log_attrs.get("app_name") or resource_attrs.get("service.name", "unknown")

                    # workflow_id: can come from multiple places
                    workflow_id = (
                        log_attrs.get("workflow_id") or
                        log_attrs.get("atlan-argo-workflow-name") or
                        log_attrs.get("trace_id")
                    )

                    # Extract application_name from resource attributes
                    application_name = resource_attrs.get("application.name", "unknown")

                    # Build the record matching Iceberg schema
                    record = {
                        # Core fields
                        "timestamp": timestamp,
                        "level": log_record.get("severityText", "INFO"),
                        "logger_name": log_attrs.get("logger_name", scope_name),
                        "message": message,

                        # Source location
                        "file": log_attrs.get("code.filepath"),
                        "line": self._safe_int(log_attrs.get("code.lineno")),
                        "function": log_attrs.get("code.function"),

                        # Legacy fields (keep for compatibility)
                        "argo_workflow_run_id": None,
                        "argo_workflow_run_uuid": None,

                        # Identity fields
                        "trace_id": log_attrs.get("trace_id", workflow_id),
                        "atlan_argo_workflow_name": workflow_id,
                        "atlan_argo_workflow_node": log_attrs.get("atlan-argo-workflow-node"),
                        "application_name": application_name,  # From resource attributes

                        # Extra struct with workflow context
                        "extra": {
                            "activity_id": log_attrs.get("activity_id"),
                            "activity_type": log_attrs.get("activity_type"),
                            "atlan-argo-workflow-name": workflow_id,
                            "atlan-argo-workflow-node": log_attrs.get("atlan-argo-workflow-node"),
                            "attempt": str(log_attrs.get("attempt", "")) if log_attrs.get("attempt") else None,
                            "client_host": log_attrs.get("client_host"),
                            "duration_ms": log_attrs.get("duration_ms"),
                            "heartbeat_timeout": log_attrs.get("heartbeat_timeout"),
                            "log_type": log_attrs.get("log_type"),
                            "method": log_attrs.get("method"),
                            "namespace": log_attrs.get("namespace"),
                            "path": log_attrs.get("path"),
                            "request_id": log_attrs.get("request_id") or log_attrs.get("atlan-request-id"),
                            "run_id": log_attrs.get("run_id"),
                            "schedule_to_close_timeout": log_attrs.get("schedule_to_close_timeout"),
                            "schedule_to_start_timeout": log_attrs.get("schedule_to_start_timeout"),
                            "start_to_close_timeout": log_attrs.get("start_to_close_timeout"),
                            "status_code": log_attrs.get("status_code"),
                            "task_queue": log_attrs.get("task_queue"),
                            "trace_id": log_attrs.get("trace_id"),
                            "url": log_attrs.get("url"),
                            "workflow_id": workflow_id,
                            "workflow_type": log_attrs.get("workflow_type"),
                        },

                        # Partition field
                        "month": self._extract_month(time_unix_nano),
                    }

                    records.append(record)

        return records

    def _safe_int(self, value: Any) -> Optional[int]:
        """Safely convert value to int, returning None on failure."""
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
