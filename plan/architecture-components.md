# Architecture Components

## Data Flow Mechanism

The new architecture will implement a continuous data flow process that decouples the extract/transform steps from the diff/publish steps:

1. **Source-to-S3 Data Flow**:
   - Workflows (extract + transform) will process data from source systems
   - Transformed metadata will be written to designated S3 prefixes
   - Each workflow run will create a timestamped folder in S3
   - Metadata about the workflow run (source, timestamp, status) will be stored in a tracking table

2. **S3-to-Publish Pipeline**:
   - A long-running service will continuously monitor the S3 prefixes
   - New data detected in S3 will trigger the diff and publish process
   - A queue-based system will manage the processing order of data batches

## Granular Details of the Diff and Publish Logic

### Timestamps and Ordering
- Each metadata batch will have:
  - Source timestamp (when data was extracted from source)
  - Processing timestamp (when data was transformed and written to S3)
  - Publish timestamp (when data was published to the metastore)
- A priority queue will ensure older data is processed before newer data from the same source
- Data will be processed according to a configurable priority scheme that balances:
  - Age of data (older data gets higher priority)
  - Source system importance (critical systems get higher priority)
  - Workflow complexity (simpler workflows get higher priority)

### Plan and Execute Pattern for Publish
The publish process will follow a two-phase approach:

1. **Planning Phase**:
   - Load previous state snapshot for comparison
   - Generate a diff between current and previous metadata
   - Create an execution plan that identifies:
     - New entities to be created
     - Existing entities to be updated
     - Entities to be deleted/archived
     - Relationships to be modified
   - Validate the execution plan for consistency
   - Store the execution plan in a durable storage

2. **Execution Phase**:
   - Load the validated execution plan
   - Break down the plan into smaller atomic operations
   - Execute operations with appropriate retry mechanisms
   - Track progress in the publish-state table
   - Upon successful completion, create a new state snapshot

## Structure of the `publish-state` Table

The publish-state table will track the lifecycle of each metadata batch as it moves through the publish process:

```python
from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum

class PublishStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    PLAN_COMPLETE = "PLAN_COMPLETE"
    EXECUTING = "EXECUTING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    RETRYING = "RETRYING"

class BatchMetrics(BaseModel):
    entities_created: int = 0
    entities_updated: int = 0
    entities_deleted: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    relationships_deleted: int = 0
    execution_time_ms: int = 0

class FailureDetails(BaseModel):
    error_code: str
    error_message: str
    failed_operation: Optional[str] = None
    stack_trace: Optional[str] = None
    retry_count: int = 0

class PublishState(BaseModel):
    batch_id: str  # Unique identifier for the batch
    source_id: str  # Identifier for the source system
    workflow_run_id: str  # Reference to the workflow run
    s3_path: str  # Location of the transformed data
    source_timestamp: datetime  # When data was extracted from source
    processing_timestamp: datetime  # When data was transformed and written to S3
    publish_start_timestamp: Optional[datetime] = None  # When publish process began
    publish_end_timestamp: Optional[datetime] = None  # When publish process completed
    status: PublishStatus = PublishStatus.PENDING
    metrics: Optional[BatchMetrics] = None
    failure_details: Optional[FailureDetails] = None
    state_snapshot_path: Optional[str] = None  # S3 path to the state snapshot
    execution_plan_path: Optional[str] = None  # S3 path to the execution plan
```

## Dead Letter Queues and Retry Mechanisms

The architecture will implement robust retry and failure handling:

1. **Retry Tiers**:
   - **Tier 1 (Immediate Retry)**: Quick retries for transient failures (network hiccups, temporary service unavailability)
   - **Tier 2 (Delayed Retry)**: Exponential backoff for service throttling or load-related issues
   - **Tier 3 (Scheduled Retry)**: Scheduled retry after system maintenance or for known issue resolution

2. **Dead Letter Queue (DLQ)**:
   - Failed batches after exhausting retries will be moved to a DLQ
   - Each DLQ entry will contain:
     - Original batch data
     - Failure reason and diagnostics
     - Retry history
   - Automated alerting for DLQ entries
   - Administrative tools to inspect, fix, and requeue DLQ entries

3. **Failure Isolation**:
   - Entity-level failure tracking to prevent entire batch failures
   - Ability to skip problematic entities and continue with the rest of the batch
   - Partial batch completion tracking

## Observability and Reporting

The architecture will include comprehensive observability capabilities:

1. **Real-time Monitoring**:
   - Dashboard showing current publish queue depth by source
   - Batch processing rates and throughput metrics
   - Error rates and patterns
   - Publish latency (time from S3 write to successful publish)

2. **Historical Analysis**:
   - Trend analysis of publish performance
   - Source system reliability metrics
   - Correlation between source data volume and publish time

3. **Alerting System**:
   - Proactive alerts for queue buildup
   - Anomaly detection for unusual error patterns
   - SLA breach predictions based on current processing rates

4. **Customer-facing Reporting**:
   - Status page showing sync status by source
   - Estimated completion times for in-progress workflows
   - Historical sync performance

5. **Operational Tooling**:
   - Ability to prioritize specific batches
   - Manual trigger for republishing specific data
   - Administrative tools for diagnosing and resolving common issues