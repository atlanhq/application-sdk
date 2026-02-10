#!/usr/bin/env python3
"""
Iceberg table configurations for workflow partitioning testing.

Defines 3 configurations:
1. workflow_name + month partition (WINNER)
2. workflow_name bucket partition
3. workflow_name bucket + month partition

All sorted by timestamp DESC.
"""

from pyiceberg.schema import Schema
from pyiceberg.types import (
    NestedField, StringType, DoubleType, LongType, StructType
)
from pyiceberg.partitioning import PartitionSpec, PartitionField
from pyiceberg.transforms import BucketTransform, IdentityTransform

def get_base_schema():
    """Get base schema (same for all configs)."""
    return Schema(
        fields=[
            NestedField(1, "timestamp", DoubleType(), required=False),
            NestedField(2, "level", StringType(), required=False),
            NestedField(3, "logger_name", StringType(), required=False),
            NestedField(4, "message", StringType(), required=False),
            NestedField(5, "file", StringType(), required=False),
            NestedField(6, "line", LongType(), required=False),
            NestedField(7, "function", StringType(), required=False),
            # REMOVED: argo_workflow_run_id, argo_workflow_run_uuid
            NestedField(10, "trace_id", StringType(), required=False),  # Set to WorkflowName
            NestedField(11, "atlan_argo_workflow_name", StringType(), required=False),
            NestedField(12, "atlan_argo_workflow_node", StringType(), required=False),
            NestedField(13, "extra", StructType(
                NestedField(14, "activity_id", StringType(), required=False),
                NestedField(15, "activity_type", StringType(), required=False),
                NestedField(16, "atlan-argo-workflow-name", StringType(), required=False),
                NestedField(17, "atlan-argo-workflow-node", StringType(), required=False),
                NestedField(18, "attempt", StringType(), required=False),
                NestedField(19, "client_host", StringType(), required=False),
                NestedField(20, "duration_ms", StringType(), required=False),
                NestedField(21, "heartbeat_timeout", StringType(), required=False),
                NestedField(22, "log_type", StringType(), required=False),
                NestedField(23, "method", StringType(), required=False),
                NestedField(24, "namespace", StringType(), required=False),
                NestedField(25, "path", StringType(), required=False),
                NestedField(26, "request_id", StringType(), required=False),
                NestedField(27, "run_id", StringType(), required=False),
                NestedField(28, "schedule_to_close_timeout", StringType(), required=False),
                NestedField(29, "schedule_to_start_timeout", StringType(), required=False),
                NestedField(30, "start_to_close_timeout", StringType(), required=False),
                NestedField(31, "status_code", StringType(), required=False),
                NestedField(32, "task_queue", StringType(), required=False),
                NestedField(33, "trace_id", StringType(), required=False),
                NestedField(34, "url", StringType(), required=False),
                NestedField(35, "workflow_id", StringType(), required=False),
                NestedField(36, "workflow_type", StringType(), required=False)
            ), required=False),
            NestedField(37, "month", LongType(), required=False),  # Month number (1-12)
        ]
    )

def create_partition_spec(schema, partition_config):
    """Create PartitionSpec from partition configuration."""
    if partition_config is None:
        return None
    
    partition_fields = []
    field_id = 1000  # Start field IDs for partition fields
    
    for item in partition_config:
        if isinstance(item, tuple):
            # Bucket transform: (field_name, 'bucket', num_buckets)
            field_name, transform_type, num_buckets = item
            # Find field ID in schema
            field = schema.find_field(field_name)
            if transform_type == 'bucket':
                transform = BucketTransform(num_buckets)
                # Partition field name must be different from schema field name
                partition_name = f"{field_name}_bucket"
                partition_fields.append(PartitionField(
                    source_id=field.field_id,
                    field_id=field_id,
                    transform=transform,
                    name=partition_name
                ))
                field_id += 1
        else:
            # Identity transform: just field name
            field_name = item
            field = schema.find_field(field_name)
            transform = IdentityTransform()
            # Partition field name must be different from schema field name
            partition_name = f"{field_name}_part"
            partition_fields.append(PartitionField(
                source_id=field.field_id,
                field_id=field_id,
                transform=transform,
                name=partition_name
            ))
            field_id += 1
    
    return PartitionSpec(*partition_fields)

CONFIGURATIONS = {
    'wf_month_partition': {
        'table_name': 'workflow_logs_wf_month',
        'schema': get_base_schema(),
        'partition_config': ['atlan_argo_workflow_name', 'month'],
        'sort_order': [('timestamp', 'DESC')],
        'description': 'Partition by workflow_name + month, sorted by timestamp DESC'
    },
    'wf_bucket': {
        'table_name': 'workflow_logs_wf_bucket',
        'schema': get_base_schema(),
        'partition_config': [('atlan_argo_workflow_name', 'bucket', 16)],  # 16 buckets
        'sort_order': [('timestamp', 'DESC')],
        'description': 'Bucket partition on workflow_name (16 buckets), sorted by timestamp DESC'
    },
    'wf_bucket_month': {
        'table_name': 'workflow_logs_wf_bucket_month',
        'schema': get_base_schema(),
        'partition_config': [('atlan_argo_workflow_name', 'bucket', 16), 'month'],
        'sort_order': [('timestamp', 'DESC')],
        'description': 'Bucket partition on workflow_name + month, sorted by timestamp DESC'
    }
}

# Create partition specs for each config
for config_name, config in CONFIGURATIONS.items():
    config['partition_spec'] = create_partition_spec(config['schema'], config['partition_config'])

def get_config(config_name):
    """Get configuration by name."""
    return CONFIGURATIONS.get(config_name)

def list_configs():
    """List all available configurations."""
    return list(CONFIGURATIONS.keys())
