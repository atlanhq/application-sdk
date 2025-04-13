I'm implementing the Execute phase for an ETL Publish workflow for Atlas metadata using Temporal and DAPR. I need a detailed implementation of this phase based on the output from the Plan phase.

CONTEXT:
The ETL Publish workflow follows a plan-execute pattern where:
1. The Planner (completed) analyzes input data and creates an execution plan
2. The Executor (this implementation) carries out the plan with no further decision-making

The Planner phase has generated a structured plan that includes:
- Asset type dependencies
- Publish order (layers of asset types to process sequentially)
- Asset type assignments with processing strategies
- Identification of self-referential types

EXECUTION REQUIREMENTS:

1. Execution Order:
   - First execute all activities for create/update operations
   - Only after all create/update operations complete, execute delete operations in parallel

2. Folder Structure:
/publish_app/
    /activities
            atlas.py - already contains plan activity, add execute activities here
    /common
        /semaphore - contains distributed lock implementation

3. Configuration Extensions:
- Add execute-specific configurations to `PublishConfig`:
  - `publish_chunk_count`: Number of assets per API call
  - `retry_config`:
     - `max_retries`: Maximum retry attempts for API calls
     - `backoff_factor`: Exponential backoff factor
     - `retry_status_codes`: List of status codes to retry
     - `fail_fast_status_codes`: List of status codes that should not be retried
  - `continue_on_error`: Boolean to determine if execution should continue on error
  - `atlas_timeout`: Timeout for Atlas API calls
  - `username`: String field for impersonation

4. Atlas API Client:
- Implement a robust Atlas API client with:
  - Authentication via Keycloak using username from config and master password from environment
  - Support for impersonation
  - Retry mechanisms with exponential backoff
  - Error handling with detailed logging
  - Methods for:
    - create_entities (bulk)
    - update_entities (bulk)
    - partial_update_entities (relationships only)
    - delete_entities

5. Main Execute Activity:
- Implement in atlas.py/def execute:
  - Takes the plan output and configuration
  - Processes asset types in the order specified by publish_order
  - Handles high-density types with file-level parallelism
  - Executes create/update operations before delete operations
  - Implements special handling for self-referential types
  - Tracks state of processed assets in DAPR object store
  - Returns comprehensive execution results

6. Processing Logic:
- Create a processor class hierarchy:
  - `BaseProcessor` with common functionality
  - `TypeLevelProcessor` for standard asset types
  - `FileLevelProcessor` for high-density asset types
  - `SelfRefProcessor` for self-referential types with two-pass approach

7. File Processing:
- For each asset type/file:
  - Read parquet files using Daft
  - Chunk records according to publish_chunk_count
  - Transform records to Atlas API format
  - Make API calls with retry logic
  - Track success/failure for each asset
  - Update state in DAPR object store

8. Self-Referential Type Handling:
- Implement two-pass approach:
  - First pass: Create entities without relationship attributes
  - Second pass: Update entities with only relationship attributes
  - Track state between passes

9. State Tracking:
- For each processed asset, store:
  - Status (SUCCESS/FAILED/PARTIAL_SUCCESS)
  - Timestamp
  - Request details
  - Response details
  - Error information if applicable
- Use DAPR object store for state persistence

10. Concurrency Control:
- Implement concurrency based on configuration:
  - Use semaphore from /common/semaphore
  - Limit concurrent activities based on max_activity_concurrency
  - Process asset types in order, but allow parallelism within a layer

11. Error Handling:
- Implement configurable error behavior:
  - continue_on_error=True: Continue with other asset types if one fails
  - continue_on_error=False: Fail the entire workflow if any asset type fails
- Log detailed error information
- Return comprehensive error reports

DETAILED IMPLEMENTATION SPECIFICATIONS:

1. Atlas Client Implementation:
```python
class AtlasClient:
    """Client for interacting with Atlas API with robust error handling and retries"""

    def __init__(self, config: PublishConfig):
        """Initialize with config including auth details"""
        self.config = config
        self.auth_token = None
        self.session = None

    async def initialize(self):
        """Initialize session with authentication"""
        # Get master password from environment
        master_password = os.environ.get("ATLAS_MASTER_PASSWORD")
        if not master_password:
            raise ValueError("ATLAS_MASTER_PASSWORD not set in environment")

        # Generate auth token with impersonation
        self.auth_token = await self._generate_auth_token(
            username=self.config.username,
            master_password=master_password
        )

        # Set up session with headers
        self.session = aiohttp.ClientSession(headers={
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "x-atlan-agent": "workflow",
            # Add other required headers
        })

    async def create_update_entities(self, entities: List[Dict[str, Any]], options: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create or update entities in bulk with retry logic"""
        # Implementation with retry and error handling

    async def create_update_relationships_only(self, entities: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Update only relationship attributes"""
        # Implementation for second pass of self-referential types

    async def delete_entities(self, guids: List[str]) -> Dict[str, Any]:
        """Delete entities by GUID"""
        # Implementation with retry and error handling

    async def _make_api_call(self, endpoint: str, method: str, payload: Dict[str, Any] = None) -> Dict[str, Any]:
        """Make API call with retry logic"""
        # Implementation with exponential backoff retry

    async def _generate_auth_token(self, username: str, master_password: str) -> str:
        """Generate auth token with impersonation"""
        # Implementation to get token from Keycloak
```

2. Processor Base Class:
```python
class BaseProcessor:
    """Base class for asset processors with common functionality"""

    def __init__(self, atlas_client: AtlasClient, config: PublishConfig, state_store):
        """Initialize with client, config and state store"""
        self.client = atlas_client
        self.config = config
        self.state_store = state_store

    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process an asset type assignment"""
        raise NotImplementedError("Subclasses must implement process()")

    async def _chunk_and_process(self, records: List[Dict[str, Any]], is_deletion: bool) -> Dict[str, Any]:
        """Chunk records and process in batches"""
        # Implementation to chunk and process records

    async def _update_state(self, file_path: str, status: str, details: Dict[str, Any]) -> None:
        """Update state in DAPR store"""
        # Implementation to update state
```


3. Type Level Processor:

```python
class TypeLevelProcessor(BaseProcessor):
    """Processor for standard asset types (type-level)"""

    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process all files for an asset type"""
        # Implementation to:
        # 1. Read all parquet files for the asset type
        # 2. Process records in chunks
        # 3. Make API calls
        # 4. Track and report status
```

4. File Level Processor:
```python
class FileLevelProcessor(BaseProcessor):
    """Processor for high-density asset types (file-level)"""

    async def process(self, file_assignment: FileAssignment, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process a single file for a high-density asset type"""
        # Implementation to:
        # 1. Read a single parquet file
        # 2. Process records in chunks
        # 3. Make API calls
        # 4. Track and report status
```

5. Self-Referential Processor:
```python
class SelfRefProcessor(BaseProcessor):
    """Processor for self-referential types with two-pass approach"""

    async def process(self, assignment: AssetTypeAssignment) -> Dict[str, Any]:
        """Process a self-referential asset type with two passes"""
        # Implementation of two-pass approach:
        # 1. First pass: Create entities without relationship attributes
        # 2. Second pass: Update entities with only relationship attributes
```

6. Main Execute Activity:

- implement the main execute activity in `activities/atlas.py/def publish`



7. Create/Update Processing:

```python
async def _process_create_update_operations(
    plan: PublishPlan,
    client: AtlasClient,
    config: PublishConfig,
    state_store
) -> Dict[str, Any]:
    """Process all create/update operations in order"""

    results = {}

    # Get non-deletion assignments
    non_deletion_assignments = [
        assignment for assignment in plan.asset_type_assignments
        if not assignment.is_deletion
    ]

    # Process in order specified by publish_order
    for layer in plan.publish_order:
        layer_assignments = [
            assignment for assignment in non_deletion_assignments
            if assignment.asset_type in layer
        ]

        # Process layer
        layer_results = await _process_layer(
            layer_assignments,
            plan.self_referential_types,
            client,
            config,
            state_store
        )

        results[f"layer_{layer}"] = layer_results

        # Check if we should continue on error
        if not config.continue_on_error and any(r.get("status") == "FAILED" for r in layer_results.values()):
            break

    return results
```

- Make reasonable assumptions for the implementation of the above code
- Make sure you look at the existing code to reuse methods/utilities whereever possible (example: common/semaphore/memory.py)

The final out each activity must me parquet but the same structure as the input data.


the input data folder structure is as @diff directory (example: /Users/nishchith/projects/apps-framework/application-sdk/diff/diff_status=DELETE/type_name=Database)

The parquet files contents looks like one in /Users/nishchith/projects/apps-framework/application-sdk/output_export.json

but we need to add the following columns to the record:
- "status": "SUCCESS", // or "FAILED"
- "error_code": null,
- "atlas_error_code": null,
- "error_description": null,
  - "request_config": {
    "api_endpoint": "/api/atlas/v2/entity/guid/12345-abcd-67890",
    "method": "DELETE",
    "retry_count": 3
  },
- "runbook_link": null