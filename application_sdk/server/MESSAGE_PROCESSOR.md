# Message Processor Implementation

## Overview

The message processor provides functionality for processing Kafka messages via Dapr input bindings. It supports both per-message and batch processing modes, with optional Temporal workflow integration.

**Key Design Decision**: The processing mode is automatically determined by `batch_size`:
- `batch_size = 1` → Per-message mode (immediate processing)
- `batch_size > 1` → Batch mode (accumulate and process)

This eliminates the need for a separate `process_mode` parameter and provides a more intuitive API.

## Architecture

### Core Components

1. **MessageProcessorConfig** (`application_sdk/server/messaging.py`)
   - Configuration for message processor behavior
   - Specifies processing mode, batch settings, and workflow integration

2. **MessageProcessor** (`application_sdk/server/messaging.py`)
   - Handles message processing logic
   - Supports per-message and batch modes
   - Optional workflow triggering
   - Built-in metrics and error handling

3. **APIServer Integration** (`application_sdk/server/fastapi/__init__.py`)
   - `register_message_processor()` - Register a processor for a Kafka binding
   - `start_message_processors()` - Start all registered processors
   - `stop_message_processors()` - Stop all registered processors

## Processing Modes

The processing mode is automatically determined by the `batch_size` parameter:

### Per-Message Mode (batch_size = 1)
```python
config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=1
)
```
- Processes each message immediately upon arrival
- No batching or delays
- Suitable for low-latency requirements
- `batch_timeout` is ignored in this mode

### Batch Mode (batch_size > 1)
```python
config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=50,
    batch_timeout=5.0
)
```
- Accumulates messages up to `batch_size`
- Processes batch when size threshold or timeout is reached
- More efficient for high-throughput scenarios

## Usage Examples

### Example 1: Basic Per-Message Processing (batch_size = 1)

```python
from application_sdk.server.messaging import MessageProcessorConfig

# Register processor in your APIServer setup
config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=1  # Per-message mode
)

async def process_messages(messages: List[dict]):
    for msg in messages:
        logger.info(f"Processing: {msg}")
        # Your custom processing logic here

server.register_message_processor(config, process_callback=process_messages)
```

### Example 2: Batch Processing with Custom Callback

```python
config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=100,  # Batch mode
    batch_timeout=10.0
)

async def process_batch(messages: List[dict]):
    # Process entire batch
    logger.info(f"Processing batch of {len(messages)} messages")
    # Bulk insert to database, etc.
    await bulk_insert_to_db(messages)

server.register_message_processor(config, process_callback=process_batch)
```

### Example 3: Workflow Integration (Per-Message)

```python
from app.workflows import MessageProcessorWorkflow

config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=1,  # Per-message: each message triggers a workflow
    trigger_workflow=True,
    workflow_class=MessageProcessorWorkflow
)

server.register_message_processor(config)
```

### Example 4: Workflow Integration (Batch)

```python
config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=50,  # Batch mode
    batch_timeout=5.0,
    trigger_workflow=True,
    workflow_class=BatchProcessorWorkflow
)

# Each batch triggers a single workflow with all messages
server.register_message_processor(config)
```

## Dapr Integration

### Binding Configuration

Create a Dapr Kafka input binding in `components/kafka-input-binding.yaml`:

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: kafka-input  # Must match config.binding_name
spec:
  type: bindings.kafka
  version: v1
  metadata:
  - name: brokers
    value: "localhost:9092"
  - name: topics
    value: "events-topic"
  - name: consumerGroup
    value: "my-consumer-group"
  - name: initialOffset
    value: "newest"
  - name: direction
    value: "input"
  - name: authType
    value: "none"
```

### Endpoint Convention

- Dapr calls `POST /{binding-name}` for each message
- Example: binding named "kafka-input" creates endpoint `POST /kafka-input`
- Statistics available at `GET /messages/v1/stats/{binding-name}`

## Monitoring & Metrics

### Built-in Metrics

The message processor automatically records:

- `kafka_messages_processed_total` - Total messages processed (counter)
  - Labels: `status` (success/error), `mode` (per_message/batch)
- `kafka_binding_requests_total` - Total binding requests (counter)
  - Labels: `status` (success/error), `binding` (binding name)
- `kafka_binding_duration_seconds` - Request duration (histogram)
  - Labels: `binding` (binding name)
- `kafka_batch_size` - Batch size distribution (histogram)

### Statistics Endpoint

Get processor stats:
```bash
curl http://localhost:3000/messages/v1/stats/kafka-input
```

Response:
```json
{
  "is_running": true,
  "current_batch_size": 15,
  "batch_size_threshold": 50,
  "batch_timeout": 5.0,
  "is_batch_mode": true,
  "total_processed": 1250,
  "total_errors": 3,
  "time_since_last_process": 2.5
}
```

## Error Handling

### Automatic Error Handling

- All exceptions are logged with full stack traces
- Error metrics are automatically recorded
- Failed messages are counted in `total_errors`
- Batch processing continues even if one message fails

### Custom Error Handling

```python
async def process_with_error_handling(messages: List[dict]):
    for msg in messages:
        try:
            await process_message(msg)
        except ValidationError as e:
            logger.error(f"Validation failed: {e}")
            # Send to dead letter queue
            await send_to_dlq(msg, str(e))
        except Exception as e:
            logger.error(f"Processing failed: {e}")
            # Retry logic
            await retry_message(msg)

config = MessageProcessorConfig(
    binding_name="kafka-input",
    batch_size=1  # Per-message mode
)
server.register_message_processor(config, process_callback=process_with_error_handling)
```

## Lifecycle Management

### Starting Processors

```python
# Start all registered message processors
await server.start_message_processors()
```

### Stopping Processors

```python
# Stop all processors and process remaining messages
await server.stop_message_processors()
```

### Integration with Application Lifecycle

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await server.start_message_processors()
    yield
    # Shutdown
    await server.stop_message_processors()

app = FastAPI(lifespan=lifespan)
```

## Performance Considerations

### Per-Message Mode (batch_size = 1)
- **Pros**: Low latency, immediate processing
- **Cons**: Higher overhead per message
- **Best for**: Real-time processing, low message volume

### Batch Mode (batch_size > 1)
- **Pros**: Better throughput, more efficient bulk operations
- **Cons**: Higher latency (up to batch_timeout)
- **Best for**: High volume, analytics, bulk database operations

### Tuning Parameters

```python
# Per-message: instant processing
batch_size=1  # batch_timeout ignored

# Low latency with small batching
batch_size=10
batch_timeout=1.0

# Balanced
batch_size=100
batch_timeout=5.0

# High throughput, can tolerate latency
batch_size=1000
batch_timeout=30.0
```

## Testing

### Unit Testing

```python
import pytest
from application_sdk.server.messaging import MessageProcessor, MessageProcessorConfig

@pytest.mark.asyncio
async def test_per_message_processing():
    config = MessageProcessorConfig(
        binding_name="test-binding",
        batch_size=1  # Per-message mode
    )
    
    processed = []
    async def callback(messages):
        processed.extend(messages)
    
    processor = MessageProcessor(config, process_callback=callback)
    
    message = {"event_type": "test", "data": {"id": 1}}
    result = await processor.add_message(message)
    
    assert result["status"] == "processed"
    assert len(processed) == 1
    assert processor.total_processed == 1
```

### Integration Testing

```python
@pytest.mark.asyncio
async def test_batch_processing():
    config = MessageProcessorConfig(
        binding_name="test-binding",
        batch_size=3,  # Batch mode
        batch_timeout=1.0
    )
    
    batches = []
    async def callback(messages):
        batches.append(messages)
    
    processor = MessageProcessor(config, process_callback=callback)
    await processor.start()
    
    # Add messages
    for i in range(3):
        await processor.add_message({"id": i})
    
    # Wait for batch processing
    await asyncio.sleep(0.1)
    
    assert len(batches) == 1
    assert len(batches[0]) == 3
    
    await processor.stop()
```

## Complete Application Example

See `/atlan-sample-apps/quickstart/simple-message-processor/` for a complete example application using the message processor with Temporal workflows.

## Troubleshooting

### Messages not being processed

1. Check Dapr is running: `dapr list`
2. Verify binding configuration matches `binding_name`
3. Check Kafka is accessible and topic exists
4. Review application logs for errors

### Batch not processing at expected time

1. Verify `batch_timeout` configuration
2. Check at least one message was received
3. Ensure processor was started: `await server.start_message_processors()`

### High memory usage

1. Reduce `batch_size` for batch mode
2. Ensure messages are being processed (check for stuck processors)
3. Monitor `current_batch_size` in stats endpoint

## References

- [Dapr Kafka Binding](https://docs.dapr.io/reference/components-reference/supported-bindings/kafka/)
- [Temporal Workflows](https://docs.temporal.io/)
- [Application SDK Documentation](../docs/)

