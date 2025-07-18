# Output Path Structure

The Application SDK uses standardized path templates for organizing both workflow outputs and state data across different storage environments (local development and cloud deployments).

## Path Templates

The SDK defines two main path templates in `application_sdk.constants`:

### Workflow Output Paths
```python
WORKFLOW_OUTPUT_PATH_TEMPLATE = "artifacts/apps/{application_name}/workflows/{workflow_id}/{run_id}"
```

This template is used for storing workflow execution outputs including:
- Data files (JSON, Parquet)
- Activity results
- Processing statistics
- Intermediate data between workflow steps

### State Store Paths
```python
STATE_STORE_PATH_TEMPLATE = "persistent-artifacts/apps/{application_name}/{state_type}/{id}/config.json"
```

This template is used for storing persistent state and configuration data including:
- Workflow state and configuration
- Activity state and progress tracking
- Application configuration data
- Persistent key-value data

## Environment-Specific Path Resolution

The actual storage location depends on your deployment environment and object store configuration:

### Local Development
When running locally with the default Dapr object store configuration:

**Workflow Outputs:**
```
./local/dapr/objectstore/artifacts/apps/my-app/workflows/wf-123/run-456/transformed/
├── tables/
│   ├── 1.json
│   ├── 2.json
│   └── statistics.json.ignore
└── columns/
    ├── 1.json
    └── statistics.json.ignore
```

**State Store:**
```
./local/dapr/objectstore/persistent-artifacts/apps/my-app/workflow/wf-123/config.json
./local/dapr/objectstore/persistent-artifacts/apps/my-app/activity/act-789/config.json
```

### Cloud Deployment (S3)
When deployed with S3 as the object store:

**Workflow Outputs:**
```
s3://my-bucket/artifacts/apps/my-app/workflows/wf-123/run-456/transformed/
├── tables/
│   ├── 1.json
│   ├── 2.json
│   └── statistics.json.ignore
└── columns/
    ├── 1.json
    └── statistics.json.ignore
```

**State Store:**
```
s3://my-bucket/persistent-artifacts/apps/my-app/workflow/wf-123/config.json
s3://my-bucket/persistent-artifacts/apps/my-app/activity/act-789/config.json
```

### Other Cloud Providers
The same path structure applies to other object store providers:

- **Azure Blob Storage:** `https://account.blob.core.windows.net/container/{path}`
- **Google Cloud Storage:** `gs://bucket-name/{path}`
- **MinIO:** `http://minio-server:9000/bucket/{path}`

## Path Components

### Workflow Output Path Components

| Component | Description | Example |
|-----------|-------------|---------|
| `application_name` | Name of your application from `APPLICATION_NAME` constant | `sql-metadata-extractor` |
| `workflow_id` | Unique identifier for the workflow type | `metadata-extraction-workflow` |
| `run_id` | Unique identifier for this specific workflow execution | `run-20241201-123456` |

### State Store Path Components

| Component | Description | Example |
|-----------|-------------|---------|
| `application_name` | Name of your application from `APPLICATION_NAME` constant | `sql-metadata-extractor` |
| `state_type` | Type of state being stored (`workflow`, `activity`, etc.) | `workflow` |
| `id` | Unique identifier for the state entity | `wf-123` |

## Usage Examples


### Saving State Data
```python
from application_sdk.outputs.statestore import StateStoreOutput
from application_sdk.inputs.statestore import StateType

# Save workflow configuration state
await StateStoreOutput.save_state(
    key="current_batch",
    value={"batch_id": 123, "processed_count": 1000},
    id="wf-metadata-extraction-456",
    type=StateType.WORKFLOW
)
# State saved to: {object_store}/{STATE_STORE_PATH_TEMPLATE}
```

## Best Practices

1. **Consistent Naming:** Use descriptive and consistent names for `application_name`, `workflow_id`, and state `id` values
2. **Path Organization:** The hierarchical structure helps organize outputs by application, workflow type, and execution
3. **State Separation:** Use appropriate `StateType` values to separate different kinds of persistent data
4. **Cleanup:** Consider implementing cleanup policies for old workflow outputs while preserving important state data
5. **Monitoring:** Use the standardized paths for monitoring and troubleshooting workflow executions

## Configuration

The path templates and object store configuration are controlled by environment variables:

```bash
# Application identification
ATLAN_APPLICATION_NAME=my-sql-extractor

# Object store configuration (varies by provider)
OBJECT_STORE_NAME=objectstore

# Local development paths
ATLAN_TEMPORARY_PATH=./local/tmp/
```

For production deployments, ensure your Dapr object store component is properly configured to point to your desired storage backend (S3, Azure Blob, GCS, etc.).