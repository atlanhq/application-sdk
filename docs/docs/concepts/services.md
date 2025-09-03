# Services

The services module provides unified interfaces for interacting with external storage, state management, secret management, and event publishing systems. These services abstract the underlying Dapr components and provide consistent, type-safe APIs for application development.

## Core Concepts

### 1. **`ObjectStore` (`application_sdk.services.objectstore`)**
   - **Purpose:** Unified interface for object storage operations supporting both file and directory operations
   - **Key Features:**
     - File upload/download operations
     - Directory-level operations with prefix support
     - Async/await support for all operations
     - Built-in cleanup and error handling
     - Support for multiple object store bindings

   **Available Methods:**
   ```python
   from application_sdk.services.objectstore import ObjectStore

   # File operations
   await ObjectStore.upload_file(source, destination, store_name=None)
   await ObjectStore.download_file(source, destination, store_name=None)
   content = await ObjectStore.get_content(key, store_name=None)
   exists = await ObjectStore.exists(key, store_name=None)

   # Directory operations
   await ObjectStore.upload_prefix(source_dir, destination_prefix, store_name=None, recursive=True)
   await ObjectStore.download_prefix(source_prefix, destination_dir=None, store_name=None)
   files = await ObjectStore.list_files(prefix="", store_name=None)

   # Note: delete method exists but is not implemented
   # await ObjectStore.delete(key, store_name=None)  # raises NotImplementedError
   ```

### 2. **`StateStore` (`application_sdk.services.statestore`)**
   - **Purpose:** Manages application state and configuration data with type-safe operations
   - **Key Features:**
     - Typed state management using `StateType` enum
     - JSON-based state storage with automatic serialization
     - Atomic state updates with conflict resolution
     - Integration with object store for persistence

   **Available Methods:**
   ```python
   from application_sdk.services.statestore import StateStore, StateType, build_state_store_path

   # State operations
   state = await StateStore.get_state(id, StateType.WORKFLOWS)
   await StateStore.save_state(key, value, id, StateType.WORKFLOWS)
   updated_state = await StateStore.save_state_object(id, state_dict, StateType.CREDENTIALS)

   # Path utilities
   path = build_state_store_path(id, StateType.WORKFLOWS)

   # StateType enum values
   StateType.WORKFLOWS  # "workflows"
   StateType.CREDENTIALS  # "credentials"
   StateType.is_member(type_string)  # Check if string is valid StateType
   ```

### 3. **`SecretStore` (`application_sdk.services.secretstore`)**
   - **Purpose:** Secure credential and secret management with multiple resolution strategies
   - **Key Features:**
     - Multiple credential sources (direct, reference-based)
     - Automatic secret substitution and resolution
     - Integration with Dapr secret store components
     - Support for nested credential structures

   **Available Methods:**
   ```python
   from application_sdk.services.secretstore import SecretStore

   # Credential operations
   credentials = await SecretStore.get_credentials(credential_guid)
   credential_guid = await SecretStore.save_secret(config_dict)

   # Secret operations
   secrets = SecretStore.get_secret(secret_key, component_name=None)
   deployment_config = SecretStore.get_deployment_secret()

   # Utility methods
   resolved = SecretStore.resolve_credentials(credential_config, secret_data)
   applied = SecretStore.apply_secret_values(source_data, secret_data)
   ```

### 4. **`EventStore` (`application_sdk.services.eventstore`)**
   - **Purpose:** Event publishing with automatic metadata enrichment and workflow context
   - **Key Features:**
     - Automatic event metadata enrichment
     - Workflow and activity context integration
     - Fallback mechanisms for different binding types
     - Authentication header injection for HTTP bindings

   **Available Methods:**
   ```python
   from application_sdk.services.eventstore import EventStore

   # Event operations
   await EventStore.publish_event(event)
   enriched_event = EventStore.enrich_event_metadata(event)
   ```

### 5. **`AtlanStorage` (`application_sdk.services.atlan_storage`)**
   - **Purpose:** Bucket cloning strategy for apps deployed in customer environments to push data to Atlan's central bucket
   - **Use Case:** When customer-deployed applications need to share their processed data with Atlan for centralized analysis or backup
   - **Key Features:**
     - High-performance parallel file migration from customer object storage to Atlan storage
     - Detailed migration reporting and error tracking
     - Integration with existing ObjectStore patterns
     - Used automatically via workflow run exit activities (not called directly)

   **Available Methods:**
   ```python
   from application_sdk.services.atlan_storage import AtlanStorage, MigrationSummary

   # Migration operations (typically called by workflow exit activities)
   summary = await AtlanStorage.migrate_from_objectstore_to_atlan(prefix="")
   # Returns MigrationSummary with migration statistics
   ```

## Comprehensive Usage Examples

### ObjectStore - Complete File and Directory Management

```python
from application_sdk.services.objectstore import ObjectStore

# =============== File Operations ===============

# Upload a single file
await ObjectStore.upload_file(
    source="local/data.json",
    destination="remote/data/file.json",
    store_name="my-store"  # Optional, uses default if not specified
)

# Download a single file
await ObjectStore.download_file(
    source="remote/data/file.json",
    destination="local/downloaded_file.json"
)

# Check if file exists
exists = await ObjectStore.exists("remote/data/file.json")
if exists:
    print("File exists in object store")

# Get file content directly
content = await ObjectStore.get_content("remote/data/file.json")
print(f"File content: {content.decode()}")

# =============== Directory Operations ===============

# Upload entire directory (recursive by default)
await ObjectStore.upload_prefix(
    source="local/data_folder",
    destination="remote/data/uploaded",
    recursive=True  # Include subdirectories
)

# Upload directory non-recursively
await ObjectStore.upload_prefix(
    source="local/data_folder",
    destination="remote/data/flat",
    recursive=False  # Only root level files
)

# Download all files from a prefix
await ObjectStore.download_prefix(
    source="remote/data/uploaded",
    destination="local/downloaded_data"
)

# List files with prefix filtering
all_files = await ObjectStore.list_files()  # All files
data_files = await ObjectStore.list_files(prefix="data/")  # Files under data/
json_files = await ObjectStore.list_files(prefix="data/json/")

# Process files in batches
files = await ObjectStore.list_files(prefix="logs/")
for file_path in files:
    if file_path.endswith('.log'):
        content = await ObjectStore.get_content(file_path)
        # Process log content...
```

### StateStore - Complete State Management

```python
from application_sdk.services.statestore import StateStore, StateType, build_state_store_path

# =============== Workflow State Management ===============

# Save individual state values
await StateStore.save_state(
    key="current_step",
    value="data_processing",
    id="workflow-123",
    type=StateType.WORKFLOWS
)

await StateStore.save_state(
    key="progress_percentage",
    value=75,
    id="workflow-123",
    type=StateType.WORKFLOWS
)

# Save complete state object
workflow_state = {
    "status": "running",
    "current_step": "transformation",
    "progress": 85,
    "started_at": "2024-01-15T10:30:00Z",
    "config": {
        "batch_size": 1000,
        "timeout": 300
    }
}

updated_state = await StateStore.save_state_object(
    id="workflow-123",
    value=workflow_state,
    type=StateType.WORKFLOWS
)

# Retrieve complete state
state = await StateStore.get_state("workflow-123", StateType.WORKFLOWS)
print(f"Current status: {state.get('status')}")
print(f"Progress: {state.get('progress')}%")

# =============== Credential State Management ===============

# Store credential configuration
cred_config = {
    "credential_type": "database",
    "host": "db.example.com",
    "port": 5432,
    "database": "prod_db"
}

await StateStore.save_state_object(
    id="db-cred-456",
    value=cred_config,
    type=StateType.CREDENTIALS
)

# Retrieve credential configuration
creds = await StateStore.get_state("db-cred-456", StateType.CREDENTIALS)

# =============== Utility Functions ===============

# Build state file paths
workflow_path = build_state_store_path("workflow-123", StateType.WORKFLOWS)
cred_path = build_state_store_path("db-cred-456", StateType.CREDENTIALS)

# Check if state type is valid
is_valid = StateType.is_member("workflows")  # True
is_invalid = StateType.is_member("invalid")  # False

# Access enum values
workflow_type = StateType.WORKFLOWS.value  # "workflows"
cred_type = StateType.CREDENTIALS.value    # "credentials"
```

### SecretStore - Complete Secret and Credential Management

```python
from application_sdk.services.secretstore import SecretStore

# =============== Credential Resolution ===============

# Get resolved credentials with automatic secret substitution
try:
    credentials = await SecretStore.get_credentials("cred-guid-123")
    print(f"Host: {credentials['host']}")
    print(f"Username: {credentials['username']}")
    # Password is automatically resolved from secret store
except Exception as e:
    print(f"Failed to resolve credentials: {e}")

# Save credentials (development only)
config = {
    "host": "localhost",
    "port": 5432,
    "username": "admin",
    "password": "secret123",
    "database": "myapp"
}
credential_guid = await SecretStore.save_secret(config)
print(f"Saved credentials with GUID: {credential_guid}")

# =============== Direct Secret Operations ===============

# Get deployment configuration
deployment_config = SecretStore.get_deployment_secret()
if deployment_config:
    print(f"Environment: {deployment_config.get('environment')}")
    print(f"Region: {deployment_config.get('region')}")

# Get specific secrets
db_secrets = SecretStore.get_secret("database-credentials")
api_secrets = SecretStore.get_secret("api-keys", "custom-secret-store")

# =============== Credential Resolution Utilities ===============

# Manual credential resolution
credential_config = {
    "host": "db.example.com",
    "username": "admin",
    "password": "password_key",  # Reference to secret
    "database": "prod"
}

secret_data = {
    "password_key": "actual_password_123"
}

resolved = SecretStore.resolve_credentials(credential_config, secret_data)
print(f"Resolved password: {resolved['password']}")  # "actual_password_123"

# Apply secret values to configuration
source_config = {
    "db_host": "localhost",
    "db_user": "user_ref",
    "api_key": "api_ref",
    "extra": {
        "backup_key": "backup_ref"
    }
}

secrets = {
    "user_ref": "actual_user",
    "api_ref": "actual_api_key",
    "backup_ref": "actual_backup_key"
}

applied_config = SecretStore.apply_secret_values(source_config, secrets)
print(f"Applied config: {applied_config}")
```

### EventStore - Event Publishing with Metadata

```python
from application_sdk.services.eventstore import EventStore
from application_sdk.events.models import Event

# =============== Basic Event Publishing ===============

# Create and publish workflow event
workflow_event = Event(
    event_type="workflow.started",
    payload={
        "workflow_id": "wf-123",
        "workflow_type": "data_processing",
        "started_by": "user@example.com"
    }
)

await EventStore.publish_event(workflow_event)

# Create activity completion event
activity_event = Event(
    event_type="activity.completed",
    payload={
        "activity_name": "data_transformation",
        "workflow_id": "wf-123",
        "duration_ms": 5432,
        "records_processed": 10000
    }
)

await EventStore.publish_event(activity_event)

# =============== Event Metadata Enrichment ===============

# Events are automatically enriched with workflow and activity context
basic_event = Event(
    event_type="custom.event",
    payload={"message": "Something happened"}
)

# This will automatically add workflow_id, activity_id, timestamps, etc.
enriched_event = EventStore.enrich_event_metadata(basic_event)
print(f"Enriched metadata: {enriched_event.metadata}")

# The enrichment happens automatically when you call publish_event
await EventStore.publish_event(basic_event)  # Will be enriched internally
```

### AtlanStorage - Bucket Cloning for Customer Deployments

**Important:** AtlanStorage is designed for apps deployed in customer environments and is typically used automatically via workflow run exit activities, not called directly in application code.

#### Use Case: Data Sharing from Customer Environments

When your application is deployed in a customer's environment, you may want to clone/share processed data back to Atlan's central storage for:
- Centralized analysis and reporting
- Data backup and archival
- Cross-customer insights (with proper permissions)
- Support and troubleshooting

#### Workflow Integration Pattern

AtlanStorage is used via the `run_exit_activities` pattern in workflows. The framework automatically calls exit activities after successful workflow completion to handle bucket cloning:

```python
# Example from BaseSQLMetadataExtractionWorkflow
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow

class MyCustomWorkflow(BaseSQLMetadataExtractionWorkflow):
    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        # Execute your normal workflow activities
        await super().run(workflow_config)

        # Framework automatically calls run_exit_activities after successful completion
        # This triggers the upload_to_atlan activity if ENABLE_ATLAN_UPLOAD is True
        # No direct AtlanStorage calls needed in your workflow code

    async def run_exit_activities(self, workflow_args: Dict[str, Any]) -> None:
        # This method is called automatically by the framework
        # It handles the AtlanStorage bucket cloning via activities
        if ENABLE_ATLAN_UPLOAD:
            await workflow.execute_activity_method(
                self.activities_cls.upload_to_atlan,  # Activity that uses AtlanStorage
                args=[workflow_args],
                retry_policy=retry_policy,
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )
```

**Configuration:** Set `ENABLE_ATLAN_UPLOAD=true` in your environment to enable automatic bucket cloning for customer deployments.

#### Direct Usage (Advanced/Testing Only)

```python
from application_sdk.services.atlan_storage import AtlanStorage, MigrationSummary

# =============== Migration Examples ===============
# Note: Typically handled automatically by workflow exit activities

# Migrate processed data files
data_summary = await AtlanStorage.migrate_from_objectstore_to_atlan(
    prefix="processed-data/"  # Only migrate processed results
)

print(f"Data Migration Summary:")
print(f"  Total files: {data_summary.total_files}")
print(f"  Successfully migrated: {data_summary.migrated_files}")
print(f"  Failed migrations: {data_summary.failed_migrations}")

if data_summary.failures:
    print("Failed files:")
    for failure in data_summary.failures:
        print(f"  - {failure['file']}: {failure['error']}")

# =============== Migration Result Processing ===============

def process_migration_results(summary: MigrationSummary) -> bool:
    """Process migration results and determine success."""
    success_rate = summary.migrated_files / summary.total_files if summary.total_files > 0 else 0

    if success_rate >= 0.95:  # 95% success threshold
        print(f"✅ Migration successful: {summary.migrated_files}/{summary.total_files} files")
        return True
    elif success_rate >= 0.8:  # 80% partial success
        print(f"⚠️  Partial migration: {summary.migrated_files}/{summary.total_files} files")
        print("Some files failed but migration mostly successful")
        return True
    else:
        print(f"❌ Migration failed: {summary.migrated_files}/{summary.total_files} files")
        return False

# Example usage in workflow exit activity
summary = await AtlanStorage.migrate_from_objectstore_to_atlan("results/")
success = process_migration_results(summary)
```

## Design Principles

1. **Unified Interface**: All services follow consistent patterns for similar operations
2. **Async First**: All I/O operations are async/await compatible
3. **Type Safety**: Strong typing with Pydantic models and enums
4. **Error Handling**: Comprehensive error handling with structured logging
5. **Resource Management**: Automatic cleanup and resource management
6. **Extensibility**: Easy to extend for new storage backends and operation types

## Integration with Other Modules

The services module integrates seamlessly with other SDK components:

- **Activities**: Services are commonly used within activities for data persistence and retrieval
- **Workflows**: State management for workflow configuration and progress tracking
- **Handlers**: Secret retrieval for database connections and API authentication
- **Outputs**: File uploads and state persistence after data processing
- **Inputs**: File downloads and credential resolution for data access
- **Run Exit Activities**: AtlanStorage is used via `run_exit_activities` pattern for automated bucket cloning in customer deployments

### Common Integration Patterns

```python
# In Activities - using multiple services together
from application_sdk.services.secretstore import SecretStore
from application_sdk.services.statestore import StateStore, StateType
from application_sdk.services.objectstore import ObjectStore

class MyActivity:
    async def process_data(self, workflow_args):
        # Get credentials
        creds = await SecretStore.get_credentials(workflow_args["credential_guid"])

        # Download input data
        await ObjectStore.download_file("input/data.json", "local/data.json")

        # Save progress state
        await StateStore.save_state(
            "progress", "50%", workflow_args["workflow_id"], StateType.WORKFLOWS
        )

        # Process data and upload results
        # ... processing logic ...

        await ObjectStore.upload_file("local/results.json", "output/results.json")

# AtlanStorage Integration Pattern (customer environment deployment)
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow

class CustomerEnvironmentWorkflow(BaseSQLMetadataExtractionWorkflow):
    @workflow.run
    async def run(self, workflow_config):
        # Normal workflow execution - process customer data
        await super().run(workflow_config)

        # Framework handles AtlanStorage bucket cloning via run_exit_activities
        # when ENABLE_ATLAN_UPLOAD is enabled in customer environment
        # This automatically clones processed results to Atlan's central bucket

# The upload_to_atlan activity is built into BaseSQLMetadataExtractionActivities
# and uses AtlanStorage for bucket cloning. Here's the actual implementation:

@activity.defn
async def upload_to_atlan(self, workflow_args: Dict[str, Any]) -> ActivityStatistics:
    """Upload transformed data to Atlan storage.

    This activity uploads the transformed data from customer object store to Atlan storage.
    It only runs if ENABLE_ATLAN_UPLOAD is set to true.
    """
    # Use workflow-specific prefix to migrate only the current workflow's data
    migration_prefix = get_object_store_prefix(workflow_args["output_path"])

    # AtlanStorage handles the bucket cloning from customer environment to Atlan
    upload_stats = await AtlanStorage.migrate_from_objectstore_to_atlan(migration_prefix)

    return ActivityStatistics(
        total_record_count=upload_stats.migrated_files,
        failed_record_count=upload_stats.failed_migrations,
        typename="atlan-upload"
    )
```

## Error Handling

All services implement consistent error handling patterns:

```python
try:
    result = await ObjectStore.get_content("nonexistent-file.json")
except Exception as e:
    logger.error(f"Failed to retrieve file: {str(e)}")
    # Handle appropriately based on context
```

## Performance Considerations

- **Parallel Operations**: Services support concurrent operations where beneficial
- **Connection Pooling**: Efficient connection management for Dapr clients
- **Batch Operations**: Optimized batch operations for multiple files/states
- **Streaming**: Large file operations use streaming where possible

The services module forms the backbone of data persistence, state management, and external system integration within the Application SDK, providing reliable, type-safe interfaces that scale with your application needs.
