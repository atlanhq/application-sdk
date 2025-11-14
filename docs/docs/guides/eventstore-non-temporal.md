# Using EventStore Without Temporal Workflows

This guide explains how to use the EventStore service in non-Temporal contexts, such as dapr bindings, event processors, and standalone applications.

## Overview

The EventStore service provides two sets of methods:

1. **Temporal Methods** (original): For use within Temporal workflows and activities
2. **Simple Methods** (new): For use in non-Temporal contexts

## Methods

### For Temporal Workflows (Original)

These methods are designed for use within Temporal workflows and activities. They automatically capture workflow/activity context.

#### `enrich_event_metadata(event: Event) -> Event`

Enriches event metadata with Temporal workflow and activity context.

```python
from application_sdk.services.eventstore import EventStore
from application_sdk.events.models import Event

# Inside a Temporal workflow or activity
event = Event(
    event_type="data.processed",
    event_name="batch_completed",
    data={"count": 100}
)

# Automatically captures workflow_id, activity_id, etc.
enriched = EventStore.enrich_event_metadata(event)
```

#### `publish_event(event: Event) -> None`

Publishes an event with Temporal context metadata.

```python
# Inside a Temporal workflow or activity
await EventStore.publish_event(event)
```

### For Non-Temporal Applications (New)

These methods are designed for standalone applications, processors, and dapr bindings that don't use Temporal.

#### `enrich_event_metadata_simple(event: Event, custom_metadata: dict = None) -> Event`

Enriches event metadata with custom information without requiring Temporal context.

```python
from application_sdk.services.eventstore import EventStore
from application_sdk.events.models import Event

# In a processor or standalone application
event = Event(
    event_type="batch.processed",
    event_name="batch_completed",
    data={"batch_size": 50}
)

# Add custom metadata
enriched = EventStore.enrich_event_metadata_simple(
    event,
    custom_metadata={
        "processor_id": "kafka-batch-1",
        "batch_id": "abc123",
        "processor_type": "kafka"
    }
)
```

#### `publish_event_simple(event: Event, custom_metadata: dict = None) -> None`

Publishes an event with custom metadata, without Temporal context.

```python
# In a processor or standalone application
await EventStore.publish_event_simple(
    event,
    custom_metadata={
        "processor_id": "kafka-batch-1",
        "source": "batch_processor"
    }
)
```

## Use Cases

### 1. Batch Event Processor

```python
from typing import List
from application_sdk.events.models import Event
from application_sdk.services.eventstore import EventStore
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

async def process_batch(events: List[Event]):
    """Process a batch of events."""
    logger.info(f"Processing {len(events)} events")
    
    # Process events
    for event in events:
        # Your processing logic
        process_event(event)
    
    # Publish summary event
    summary = Event(
        event_type="processor_events",
        event_name="batch_processed",
        data={
            "batch_size": len(events),
            "event_types": list(set(e.event_type for e in events))
        }
    )
    
    await EventStore.publish_event_simple(
        summary,
        custom_metadata={
            "processor_type": "kafka_batch",
            "batch_id": generate_batch_id()
        }
    )
```

### 2. Dapr Binding Handler

```python
from fastapi import Request
from application_sdk.events.models import Event
from application_sdk.services.eventstore import EventStore

async def handle_kafka_event(request: Request):
    """Handle event from Kafka via dapr binding."""
    body = await request.json()
    event = Event(**body)
    
    # Process the event
    result = await process_event(event)
    
    # Publish result event
    result_event = Event(
        event_type="processing_results",
        event_name="event_processed",
        data=result
    )
    
    await EventStore.publish_event_simple(
        result_event,
        custom_metadata={"handler": "kafka_binding"}
    )
```

### 3. Standalone Event Publisher

```python
import asyncio
from application_sdk.events.models import Event
from application_sdk.services.eventstore import EventStore

async def publish_metrics():
    """Publish metrics as events."""
    metrics = collect_metrics()
    
    event = Event(
        event_type="system_metrics",
        event_name="metrics_collected",
        data=metrics
    )
    
    await EventStore.publish_event_simple(
        event,
        custom_metadata={
            "source": "metrics_collector",
            "hostname": get_hostname()
        }
    )

# Run standalone
asyncio.run(publish_metrics())
```

## Custom Metadata Fields

The `custom_metadata` parameter can include any fields that exist on the `EventMetadata` model:

```python
custom_metadata = {
    # Standard fields
    "workflow_id": "custom-workflow-123",
    "workflow_type": "custom_processor",
    "workflow_state": "running",
    
    # Custom fields (if added to EventMetadata)
    "processor_id": "kafka-1",
    "batch_id": "batch-123",
    "source_system": "kafka",
}
```

## Best Practices

### 1. Use Appropriate Method

- **Temporal context**: Use `publish_event()` and `enrich_event_metadata()`
- **Non-Temporal context**: Use `publish_event_simple()` and `enrich_event_metadata_simple()`

### 2. Include Meaningful Metadata

```python
# Good - descriptive metadata
custom_metadata = {
    "processor_type": "kafka_batch",
    "processor_id": f"kafka-{instance_id}",
    "batch_id": batch_id,
    "processing_duration_ms": duration
}

# Not ideal - minimal context
custom_metadata = {"id": "123"}
```

### 3. Handle Failures Gracefully

```python
try:
    await EventStore.publish_event_simple(event, custom_metadata)
except Exception as e:
    # Log but don't fail the main operation
    logger.warning(f"Failed to publish event: {e}")
```

### 4. Use Consistent Naming

```python
# Event types should be consistent
event_type = "processor_events"  # Good
event_type = "ProcessorEvents"   # Inconsistent

# Event names should be descriptive
event_name = "batch_processed"   # Good
event_name = "done"              # Too vague
```

## Differences from Temporal Methods

| Feature | Temporal Methods | Simple Methods |
|---------|-----------------|----------------|
| Workflow context | Automatically captured | Not captured |
| Activity context | Automatically captured | Not captured |
| Custom metadata | Not supported | Supported via parameter |
| Use case | Temporal workflows/activities | Processors, bindings, standalone |
| Dependencies | Requires Temporal | No Temporal required |

## Migration Example

If you have a processor that was using Temporal methods:

**Before (requires Temporal):**
```python
# Won't work outside Temporal context
await EventStore.publish_event(event)
```

**After (works everywhere):**
```python
# Works in any context
await EventStore.publish_event_simple(
    event,
    custom_metadata={"processor_id": "my-processor"}
)
```

## Troubleshooting

### Event Not Published

Check if the eventstore component is registered:

```python
from application_sdk.common.dapr_utils import is_component_registered
from application_sdk.constants import EVENT_STORE_NAME

if not is_component_registered(EVENT_STORE_NAME):
    logger.warning("EventStore component not registered")
```

### Missing Metadata

Verify your custom metadata fields exist on EventMetadata:

```python
from application_sdk.events.models import EventMetadata

# Check available fields
print(EventMetadata.model_fields.keys())
```

### Authentication Errors

Ensure your application has proper authentication configured:

```python
from application_sdk.clients.atlan_auth import AtlanAuthClient

auth_client = AtlanAuthClient()
headers = await auth_client.get_authenticated_headers()
```

## See Also

- [EventStore API Reference](../concepts/eventstore.md)
- [Event Models](../concepts/events.md)
- [Dapr Bindings](https://docs.dapr.io/developing-applications/building-blocks/bindings/)
- [Simple Kafka Processor Example](../../../atlan-sample-apps/quickstart/simple-kafka-processor/)


