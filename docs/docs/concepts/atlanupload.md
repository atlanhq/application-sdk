# Atlan Upload Activity

This document describes the new Atlan upload activity that has been added to the Application SDK.

## Overview

The Atlan upload activity (`upload_to_atlan`) is a new step in the SQL metadata extraction workflow that uploads transformed data from the **object store** to Atlan storage (S3 via Dapr). This activity is designed to run only when extraction is happening outside of Atlan, providing a sync step between object store and Atlan storage.

## Key Features

### 🔄 **Object Store to Atlan Replication**
- **Exact replication** from local object store to Atlan storage
- **No state store dependencies** - data is read directly from object store
- **Automatic file discovery** using object store listing

### 📦 **Multipart Upload Support**
- **Automatic multipart upload** for files larger than 5MB
- **Configurable chunk sizes** (default: 5MB chunks)
- **Resilient upload** with proper error handling

### 🎯 **Conditional Execution**
- **Environment-controlled** - only runs when `ENABLE_ATLAN_UPLOAD=true`
- **Component-aware** - checks for Atlan storage component availability
- **Graceful degradation** - doesn't fail workflows if upload is disabled

## Configuration

### Environment Variables

The Atlan upload functionality is controlled by the following environment variables:

- `ENABLE_ATLAN_UPLOAD`: Set to `true` to enable the Atlan upload activity (default: `false`)
- `UPSTREAM_OBJECT_STORE_NAME`: Name of the Dapr component for upstream object store (default: `objectstore`)
- `DEPLOYMENT_OBJECT_STORE_NAME`: Name of the Dapr component for deployment object store (default: `objectstore`)

### Example Configuration

```bash
# Enable Atlan upload
export ENABLE_ATLAN_UPLOAD=true

# Customize upstream object store component name (optional)
export UPSTREAM_OBJECT_STORE_NAME=my-upstream-objectstore

# Customize deployment object store component name (optional)
export DEPLOYMENT_OBJECT_STORE_NAME=my-deployment-objectstore
```

## Architecture

### Data Flow

```
1. Transform Data Activity
   ↓ (stores in object store)
2. Object Store (local)
   ↓ (reads from object store)
3. Upload to Atlan Activity
   ↓ (uploads to S3)
4. Atlan Storage (S3 via Dapr)
```

### Object Store Integration

The system uses **object store listing** to discover transformed data:

- **File Pattern**: `{workflow_id}_transformed_{data_type}_{chunk_index}.json`
- **Prefix Filtering**: Uses `{workflow_id}_transformed` prefix to find relevant files
- **Direct Upload**: Downloads file content and uploads to Atlan storage

### Multipart Upload Logic

- **Threshold**: 5MB (configurable via `MULTIPART_UPLOAD_THRESHOLD`)
- **Chunk Size**: 5MB (configurable via `MULTIPART_CHUNK_SIZE`)
- **Naming**: `{atlan_path}.part{001,002,003,...}`

## Usage

### Basic Usage

The activity is automatically included in the workflow when `ENABLE_ATLAN_UPLOAD=true`:

```python
# The activity runs automatically after transform_data
workflow_activities = [
    activities.preflight_check,
    activities.fetch_databases,
    activities.fetch_schemas,
    activities.fetch_tables,
    activities.fetch_columns,
    activities.transform_data,
    activities.upload_to_atlan,  # ← NEW: Only if ENABLE_ATLAN_UPLOAD=true
]
```

### Custom Atlan Prefix

You can customize the Atlan storage prefix in your workflow configuration:

```python
workflow_config = {
    "workflow_id": "my-workflow",
    "atlan_prefix": "custom/atlan/path/transformed",  # Optional
    # ... other config
}
```

If not specified, the default prefix is: `atlan/{workflow_id}/{workflow_run_id}/transformed`

## Dapr Component Configuration

### Upstream Object Store Component

Create a Dapr component configuration file (`components/upstream-objectstore.yaml`):

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: upstream-objectstore
spec:
  type: bindings.aws.s3
  version: v1
  metadata:
    - name: accessKey
      value: {{clienid}}
    - name: secretKey
      value: {{clienid}}
    - name: endpoint
      value: "https://{{tenant_name}}/api/s3proxy"
    - name: forcePathStyle
      value: "false"
    - name: enableMultipartUpload
      value: "true"
```

### Deployment Object Store Component

Ensure you have a deployment object store component configured:

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: deployment-objectstore
spec:
  type: bindings.localstorage
  version: v1
  metadata:
    - name: rootPath
      value: "/tmp/objectstore"
```

### AWS Secrets Component

Ensure you have AWS credentials configured in your secret store:

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: aws-secrets
spec:
  type: secretstores.aws.secretmanager
  version: v1
  metadata:
    - name: region
      value: "ap-south-1"
    - name: accessKey
      value: "your-access-key"
    - name: secretKey
      value: "your-secret-key"
```

## API Reference

### AtlanStorageOutput

#### `upload_object_store_data_to_atlan()`

Uploads data from object store to Atlan storage with multipart support.

```python
upload_stats = await AtlanStorageOutput.upload_object_store_data_to_atlan(
    workflow_id="my-workflow",
    workflow_run_id="run-123",
    atlan_prefix="atlan/transformed",
    object_store_prefix="my-workflow_transformed"  # Optional
)
```

**Returns**: Dictionary with upload statistics:
- `total_files`: Total number of files found
- `uploaded_files`: Number of successfully uploaded files
- `failed_files`: Number of failed uploads
- `total_bytes`: Total bytes uploaded
- `errors`: List of error messages

### JsonOutput

#### `write_daft_dataframe()`

Stores a Daft DataFrame in the object store and local files.

## Testing

### Run the Test Suite

```bash
python test_atlan_upload.py
```

The test suite covers:
- Object store data creation
- Atlan storage availability checking
- Object store to Atlan upload
- Multipart upload functionality
- JsonOutput functionality

### Manual Testing

1. **Enable Atlan upload**:
   ```bash
   export ENABLE_ATLAN_UPLOAD=true
   ```

2. **Start Dapr sidecar**:
   ```bash
   make dapr-sidecar
   ```

3. **Run your workflow** - the upload activity will execute automatically

4. **Check Atlan storage** for uploaded files

## Error Handling

### Graceful Degradation

The activity is designed to **never fail the workflow**:

- **Upload disabled**: Returns skip statistics
- **Component unavailable**: Returns skip statistics with warning
- **Upload errors**: Returns error statistics but continues workflow

### Error Types

- `atlan-upload-disabled`: Upload is disabled via configuration
- `atlan-upload-skipped`: Component not available
- `atlan-upload-completed`: Successful upload
- `atlan-upload-error`: Upload failed (but workflow continues)

## Performance Considerations

### Multipart Upload Benefits

- **Large file support**: Handles files >5MB efficiently
- **Parallel uploads**: Multiple chunks can be uploaded simultaneously
- **Resume capability**: Failed chunks can be retried independently

### Object Store Considerations

- **File discovery**: Uses object store listing to find files
- **Memory usage**: Large datasets are chunked to avoid memory issues
- **Cleanup**: Old data can be cleaned up manually

## Troubleshooting

### Common Issues

1. **Upload not running**:
   - Check `ENABLE_ATLAN_UPLOAD=true`
   - Verify Atlan storage component is configured

2. **Component not available**:
   - Ensure Dapr sidecar is running
   - Check component configuration
   - Verify AWS credentials

3. **Upload failures**:
   - Check S3 bucket permissions
   - Verify network connectivity
   - Review error logs for specific issues

### Debug Logging

Enable debug logging to see detailed upload information:

```python
import logging
logging.getLogger("application_sdk.outputs.atlan_storage").setLevel(logging.DEBUG)
```

## Migration Guide

### From Previous Versions

If you're upgrading from a previous version of the Application SDK:

1. **No code changes required** - The upload activity is automatically included when enabled

2. **Enable Atlan upload** (if not already enabled):
   ```bash
   export ENABLE_ATLAN_UPLOAD=true
   ```

3. **Configure Dapr components** - Ensure you have the required components:
   - `upstream-objectstore` component for S3 uploads
   - `deployment-objectstore` component for local storage
   - `aws-secrets` component for AWS credentials

4. **Verify configuration** - The upload activity will automatically:
   - Check for component availability
   - Upload data from object store to Atlan storage
   - Handle errors gracefully without failing the workflow

### From Manual Upload to Automated Upload

If you were previously manually uploading data to Atlan:

1. **Remove manual upload code** - The activity handles this automatically

2. **Configure environment variables**:
   ```bash
   export ENABLE_ATLAN_UPLOAD=true
   export UPSTREAM_OBJECT_STORE_NAME=upstream-objectstore
export DEPLOYMENT_OBJECT_STORE_NAME=deployment-objectstore
   ```

3. **The workflow will now automatically upload** after the transform step

The upload activity will automatically handle the rest!

## Future Enhancements

- **S3 multipart API integration** for better multipart upload support
- **Compression support** for large datasets
- **Incremental upload** to avoid re-uploading unchanged data
- **Upload progress tracking** for long-running uploads