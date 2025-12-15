# Atlan Upload: Technical Deep Dive

## Overview

The `ENABLE_ATLAN_UPLOAD` feature enables automatic synchronization of transformed metadata from local object storage to Atlan's central storage system (S3 via Dapr). This document provides a comprehensive technical explanation of the storage architecture, data flow, and how this feature works.

## Storage Architecture

The Application SDK uses a three-tier storage architecture:

### 1. **Local Storage (Deployment Object Store)**

**Component Name:** `DEPLOYMENT_OBJECT_STORE_NAME` (default: `"objectstore"`)
M
**Purpose:**
- Primary storage for workflow execution
- Stores raw extracted data, transformed data, and intermediate artifacts
- Located in the customer's deployment environment
- Used for all workflow operations by default

**Characteristics:**
- Fast, local access for workflow activities
- Temporary storage for processing
- Can be any Dapr-supported object store (S3, Azure Blob, local filesystem, etc.)
- Configured via Dapr component: `components/objectstore.yaml`

**Usage:**
- Activities read/write data during execution
- Transform operations write results here
- Parquet outputs are stored here
- JSON outputs are stored here

### 2. **Upstream Object Store**

**Component Name:** `UPSTREAM_OBJECT_STORE_NAME` (default: `"objectstore"`)

**Purpose:**
- Intermediate storage layer
- Used when `ENABLE_ATLAN_UPLOAD=true` to stage data before Atlan upload
- Can be the same as deployment object store or a separate component
- Acts as a buffer for data being prepared for Atlan

**Characteristics:**
- Optional intermediate layer
- Used during parquet output processing when Atlan upload is enabled
- Files are uploaded here first, then migrated to Atlan storage

**Usage:**
- When `ENABLE_ATLAN_UPLOAD=true`, parquet files are uploaded to upstream store
- Data is then migrated from upstream store to Atlan storage

### 3. **Atlan Storage**

**Component Name:** `UPSTREAM_OBJECT_STORE_NAME` (when configured for Atlan)

**Purpose:**
- Centralized Atlan storage system (S3 via Dapr)
- Destination for transformed metadata when running outside Atlan
- Used for centralized analysis, backup, and data synchronization
- Configured via Dapr component: `components/atlan-storage.yaml`

**Characteristics:**
- Atlan-managed S3 bucket
- Access via Atlan API endpoint: `https://{{tenant}}.atlan.com/api/blobstorage`
- Requires Atlan authentication credentials
- Used for bucket cloning strategy in customer deployments

**Configuration:**
```yaml
# components/atlan-storage.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: atlan-objectstore  # This becomes UPSTREAM_OBJECT_STORE_NAME
spec:
  type: bindings.aws.s3
  metadata:
    - name: bucket
      value: atlan-bucket
    - name: endpoint
      value: https://{{tenant}}.atlan.com/api/blobstorage
    - name: accessKey
      secretKeyRef:
        name: ATLAN_AUTH_CLIENT_ID
    - name: secretKey
      secretKeyRef:
        name: ATLAN_AUTH_CLIENT_SECRET
```

## Default Behavior (`ENABLE_ATLAN_UPLOAD=false`)

When `ENABLE_ATLAN_UPLOAD` is disabled (default), the workflow operates in **local-only mode**:

### Workflow Execution Flow

```
1. Preflight Check
   ↓
2. Fetch Databases
   ↓
3. Fetch Schemas
   ↓
4. Fetch Tables
   ↓
5. Fetch Columns
   ↓
6. Fetch Procedures
   ↓
7. Transform Data
   ↓
8. [upload_to_atlan SKIPPED]
   ↓
9. Workflow Complete
```

### Data Storage Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    Workflow Activities                       │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│         Local Storage (Deployment Object Store)             │
│                                                              │
│  • Raw extracted data                                        │
│  • Transformed JSON files                                    │
│  • Parquet files                                             │
│  • Intermediate artifacts                                    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
                    [Data Stays Local]
              (No upload to Atlan storage)
```

### Key Characteristics

1. **No Atlan Upload Activity**: The `upload_to_atlan` activity is included in the workflow but skipped during execution
2. **Local Storage Only**: All data remains in the deployment object store
3. **No Upstream Upload**: Parquet files are not uploaded to upstream store
4. **Workflow Completes**: Workflow completes successfully without any Atlan synchronization

### Code Behavior

In `workflows/metadata_extraction/sql.py`:
```python
# upload_to_atlan activity is in the list but conditionally executed
base_activities = [
    activities.preflight_check,
    activities.fetch_databases,
    # ... other activities ...
    activities.transform_data,
    activities.upload_to_atlan,  # Included but skipped if disabled
]
```

In `workflows/metadata_extraction/sql.py` exit activities:
```python
if ENABLE_ATLAN_UPLOAD:
    # Execute upload_to_atlan activity
    await workflow.execute_activity_method(...)
else:
    logger.info("Atlan upload skipped for workflow (disabled)")
```

## Enabled Behavior (`ENABLE_ATLAN_UPLOAD=true`)

When `ENABLE_ATLAN_UPLOAD` is enabled, the workflow operates in **Atlan synchronization mode**:

### Workflow Execution Flow

```
1. Preflight Check
   ↓
2. Fetch Databases
   ↓
3. Fetch Schemas
   ↓
4. Fetch Tables
   ↓
5. Fetch Columns
   ↓
6. Fetch Procedures
   ↓
7. Transform Data
   ↓
8. Upload to Atlan [EXECUTED]
   ↓
9. Workflow Complete
```

### Data Storage Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    Workflow Activities                       │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│         Local Storage (Deployment Object Store)             │
│                                                              │
│  • Raw extracted data                                        │
│  • Transformed JSON files                                    │
│  • Parquet files                                             │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼ (when ENABLE_ATLAN_UPLOAD=true)
┌─────────────────────────────────────────────────────────────┐
│         Upstream Object Store (Optional)                     │
│                                                              │
│  • Parquet files (staged)                                    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼ (upload_to_atlan activity)
┌─────────────────────────────────────────────────────────────┐
│              Atlan Storage (S3 via Dapr)                     │
│                                                              │
│  • Transformed JSON files                                    │
│  • Parquet files                                             │
│  • Centralized Atlan storage                                 │
└─────────────────────────────────────────────────────────────┘
```

### Detailed Upload Process

#### Step 1: Transform Data Activity

The `transform_data` activity writes transformed data to local storage:

```python
# activities/metadata_extraction/sql.py
async def transform_data(self, workflow_args: Dict[str, Any]):
    # Transform metadata to Atlas format
    # Write to local object store
    # Files stored as: {workflow_id}_transformed_{data_type}_{chunk_index}.json
```

**Output Location:** `{output_path}/transformed/{typename}/`

#### Step 2: Parquet Output Processing (Optional)

When parquet output is enabled and `ENABLE_ATLAN_UPLOAD=true`:

```python
# outputs/parquet.py
if ENABLE_ATLAN_UPLOAD:
    # Upload to upstream object store first
    await ObjectStore.upload_file(
        source=path,
        store_name=UPSTREAM_OBJECT_STORE_NAME,
        destination=get_object_store_prefix(path),
        retain_local_copy=True,
    )
# Then upload to local deployment store
await ObjectStore.upload_file(
    source=path,
    destination=get_object_store_prefix(path),
    retain_local_copy=self.retain_local_copy,
)
```

#### Step 3: Upload to Atlan Activity

The `upload_to_atlan` activity migrates data from local storage to Atlan storage:

```python
# activities/metadata_extraction/sql.py
async def upload_to_atlan(self, workflow_args: Dict[str, Any]):
    # Get prefix for transformed data
    migration_prefix = get_object_store_prefix(workflow_args["output_path"])

    # Migrate all files under this prefix
    upload_stats = await AtlanStorage.migrate_from_objectstore_to_atlan(
        prefix=migration_prefix
    )
```

**Migration Process:**

1. **File Discovery**: Lists all files in local object store under the prefix
2. **Parallel Migration**: Migrates files concurrently for performance
3. **File Transfer**: For each file:
   - Reads file content from local object store (`DEPLOYMENT_OBJECT_STORE_NAME`)
   - Uploads to Atlan storage via Dapr binding (`UPSTREAM_OBJECT_STORE_NAME`)
4. **Error Handling**: Tracks successes and failures, continues on errors
5. **Statistics**: Returns migration summary with counts and error details

**Migration Implementation:**

```python
# services/atlan_storage.py
async def migrate_from_objectstore_to_atlan(prefix: str):
    # 1. List files from local object store
    files_to_migrate = await ObjectStore.list_files(
        prefix, store_name=DEPLOYMENT_OBJECT_STORE_NAME
    )

    # 2. Create parallel migration tasks
    migration_tasks = [
        asyncio.create_task(_migrate_single_file(file_path))
        for file_path in files_to_migrate
    ]

    # 3. Execute all migrations in parallel
    results = await asyncio.gather(*migration_tasks)

    # 4. Return migration summary
    return MigrationSummary(...)
```

**Single File Migration:**

```python
async def _migrate_single_file(file_path: str):
    # 1. Read from local object store
    file_data = await ObjectStore.get_content(
        file_path, store_name=DEPLOYMENT_OBJECT_STORE_NAME
    )

    # 2. Upload to Atlan storage via Dapr binding
    with DaprClient() as client:
        client.invoke_binding(
            binding_name=UPSTREAM_OBJECT_STORE_NAME,  # Atlan storage component
            operation="create",
            data=file_data,
            binding_metadata={"key": file_path},
        )
```

### Key Characteristics

1. **Automatic Upload**: `upload_to_atlan` activity executes automatically
2. **Exact Replication**: All transformed files are replicated to Atlan storage
3. **Parallel Processing**: Files are migrated concurrently for performance
4. **Error Resilience**: Workflow continues even if some files fail to upload
5. **No State Dependencies**: Migration reads directly from object store
6. **Prefix-Based**: Only files under the workflow's output prefix are migrated

## How to Enable Atlan Upload

### Step 1: Set Environment Variable

```bash
export ENABLE_ATLAN_UPLOAD=true
```

Or in your deployment configuration:

```yaml
# Kubernetes Deployment
env:
  - name: ENABLE_ATLAN_UPLOAD
    value: "true"
```

### Step 2: Configure Atlan Storage Component

Create or update `components/atlan-storage.yaml`:

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: atlan-objectstore  # Must match UPSTREAM_OBJECT_STORE_NAME
spec:
  type: bindings.aws.s3
  version: v1
  metadata:
    - name: bucket
      value: atlan-bucket
    - name: region
      value: us-east-1
    - name: endpoint
      value: https://{{tenant}}.atlan.com/api/blobstorage
    - name: accessKey
      secretKeyRef:
        name: ATLAN_AUTH_CLIENT_ID
        key: ATLAN_AUTH_CLIENT_ID
    - name: secretKey
      secretKeyRef:
        name: ATLAN_AUTH_CLIENT_SECRET
        key: ATLAN_AUTH_CLIENT_SECRET
    - name: forcePathStyle
      value: "true"
auth:
  secretStore: deployment-secret-store
```

### Step 3: Configure Upstream Object Store Name

If using a different component name for Atlan storage:

```bash
export UPSTREAM_OBJECT_STORE_NAME=atlan-objectstore
```

### Step 4: Verify Component Registration

The system automatically checks if the Atlan storage component is registered. If not available, the upload activity will skip gracefully with a warning.

### Step 5: Run Workflow

The workflow will automatically:
1. Execute all normal activities
2. Transform data to local storage
3. Upload transformed data to Atlan storage
4. Complete successfully

## Technical Implementation Details

### Activity Execution

The `upload_to_atlan` activity is included in the workflow activity list but conditionally executed:

```python
# workflows/metadata_extraction/sql.py
def get_activities(activities):
    base_activities = [
        # ... other activities ...
        activities.upload_to_atlan,  # Always in list
    ]
    return base_activities
```

Execution is controlled in the workflow's exit activities:

```python
# workflows/metadata_extraction/sql.py
async def run_exit_activities(self, workflow_args: Dict[str, Any]):
    if ENABLE_ATLAN_UPLOAD:
        await workflow.execute_activity_method(
            self.activities_cls.upload_to_atlan,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
    else:
        logger.info("Atlan upload skipped for workflow (disabled)")
```

### Migration Summary

The upload activity returns a `MigrationSummary` with detailed statistics:

```python
class MigrationSummary(BaseModel):
    total_files: int = 0          # Total files found
    migrated_files: int = 0       # Successfully migrated
    failed_migrations: int = 0    # Failed migrations
    failures: List[Dict[str, str]] = []  # Detailed failure info
    prefix: str = ""              # Prefix used for migration
    source: str = DEPLOYMENT_OBJECT_STORE_NAME
    destination: str = UPSTREAM_OBJECT_STORE_NAME
```

### Error Handling

The upload activity is designed to **never fail the workflow**:

- **Upload Disabled**: Returns skip statistics, workflow continues
- **Component Unavailable**: Returns skip statistics with warning, workflow continues
- **Upload Errors**: Returns error statistics but workflow continues
- **Partial Failures**: Some files may fail, but successful uploads are reported

### Performance Considerations

1. **Parallel Migration**: Files are migrated concurrently using `asyncio.gather()`
2. **No Blocking**: Upload happens asynchronously, doesn't block other activities
3. **Efficient Transfer**: Direct Dapr binding invocation, no intermediate storage
4. **Large File Support**: Handles files of any size through Dapr bindings

## Use Cases

### Customer Deployment Scenario

When applications are deployed in customer environments but need to sync data to Atlan:

1. **Extraction**: Metadata extracted in customer environment
2. **Transformation**: Data transformed locally
3. **Upload**: Transformed data automatically uploaded to Atlan storage
4. **Centralized Processing**: Atlan can process data from multiple customer deployments

### Development/Testing Scenario

When testing the full workflow including Atlan integration:

1. **Local Development**: Run workflows locally
2. **Enable Upload**: Set `ENABLE_ATLAN_UPLOAD=true`
3. **Test Integration**: Verify data flows to Atlan storage
4. **Validate**: Check that files appear in Atlan storage

## Troubleshooting

### Upload Not Running

**Symptoms:** No upload activity execution

**Checks:**
1. Verify `ENABLE_ATLAN_UPLOAD=true` is set
2. Check workflow logs for "Atlan upload skipped" message
3. Verify environment variable is set correctly

### Component Not Available

**Symptoms:** Upload skipped with component warning

**Checks:**
1. Verify Dapr sidecar is running
2. Check `components/atlan-storage.yaml` exists
3. Verify component name matches `UPSTREAM_OBJECT_STORE_NAME`
4. Check Dapr component registration: `dapr components list`

### Upload Failures

**Symptoms:** Migration summary shows failed migrations

**Checks:**
1. Verify Atlan storage credentials are correct
2. Check S3 bucket permissions
3. Verify network connectivity to Atlan endpoint
4. Review error details in `MigrationSummary.failures`
5. Check Dapr binding logs for specific errors

### No Files Found

**Symptoms:** Migration summary shows `total_files: 0`

**Checks:**
1. Verify transform_data activity completed successfully
2. Check output path in workflow_args
3. Verify files exist in local object store under the prefix
4. Check object store listing permissions

## Summary

The `ENABLE_ATLAN_UPLOAD` feature provides a seamless way to synchronize transformed metadata from local deployments to Atlan's centralized storage system. By understanding the three-tier storage architecture (Local → Upstream → Atlan) and the conditional execution flow, you can effectively configure and troubleshoot the Atlan upload functionality.

**Key Takeaways:**
- **Default**: Data stays in local storage only
- **Enabled**: Data is automatically replicated to Atlan storage
- **Architecture**: Three-tier storage system with optional upstream layer
- **Resilience**: Upload failures don't fail the workflow
- **Performance**: Parallel migration for efficient data transfer


