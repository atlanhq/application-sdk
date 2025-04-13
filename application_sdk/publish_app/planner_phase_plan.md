I'm implementing an ETL Publish workflow for Atlas metadata using Temporal and DAPR. I need a detailed implementation of the Plan phase.

CONTEXT:
The ETL Publish workflow follows a plan-execute pattern where:
1. The Planner (Plan phase) analyzes input data and creates an execution plan
2. The Executor (Execute phase) carries out the plan with no further decision-making

DETAILED REQUIREMENTS FOR THE PLAN PHASE:

1. Folder Structure:
- publish_app/
    /activities
        atlas.py - this is where the plan and publish activities will be implemented

2. Configuration Classes:
- `PublishConfig` class containing:
  - `concurrency_activities`: Number of parallel activities to spawn
  - `max_activity_concurrency`: Maximum concurrency per worker
  - `username`: Username for impersonation
  - `high_density_asset_types`: List of asset types to be treated as high-density
  - State store configuration
  - Lock configuration - use the in-memory lock for now (this is implemented under /common/semaphore)
  - Atlas API configuration

3. Plan Output Contract:
- Create dataclasses that represent the planner's output contract:
  - `FileAssignment`: For high-density types, contains file_path, estimated_records
  - `AssetTypeAssignment`: Contains asset_type, directory_prefix, file_count, total_records, processing_strategy, diff_status
  - `PublishPlan`: Top-level contract with:
    - `dependency_graph`: Dict mapping types to their dependencies
    - `publish_order`: List of lists (layers) of asset types - derived from dependency graph
    - `asset_type_assignments`: List of assignments
    - `self_referential_types`: Set of types needing special handling
    - Serialization methods (to_dict/from_dict)
    - NOTE: Chunk count is NOT part of the plan phase, it will be passed in the execute phase

4. Directory Discovery for Daft DataFrames:
- Implement functions to:
  - Scan input directories to find parquet files in the format: `/Users/nishchith/projects/apps-framework/application-sdk/diff/diff_status=DELETE/type_name=Database`
  - Use Daft DataFrame to read parquet files and count records
  - Process diff_status in planning logic:
    - Ignore NO_DIFF
    - Treat DIFF and NEW as create operations
    - Treat DELETE as deletion operations
  - Return structured information about discovered asset types

5. Dependency Analysis:
- Use NetworkX for graph operations
- Implement functions to:
  - Get typedefs from pyatlan for discovered types
  - Build a dependency graph between asset types
  - Identify self-referential types by analyzing attributes
  - Determine optimal publish order using topological sort
  - Handle cyclic dependencies with fallback to manual order
  - Generate publish_order (execution layers) from the dependency graph

6. Main Planner Activity:
- Implement the Temporal activity - atlas.py/def plan
  - Takes input data path and configuration
  - Calls discovery functions to scan directories
  - Builds dependency graph and identifies self-references
  - Determines publish_order by topological layers
  - Creates asset type assignments with processing strategies
  - Determines high-density types that need file-level processing
  - Assembles and returns the execution plan contract

SPECIFIC IMPLEMENTATION DETAILS:
- Use NetworkX for graph algorithms
- Make the planner support automatic ordering
- Use dataclasses for clean object modeling
- Use async/await for asynchronous operations
- Implement proper error handling with descriptive messages
- Add detailed logging throughout the process
- Include type hints for all functions and classes
- For high-density asset types (from config), set processing_strategy="file_level"
- For normal asset types, set processing_strategy="type_level"
- When deserializing the plan, ensure all data types match expectations

Example plan output:
```json
{
  "dependency_graph": {
    "Database": [],
    "Schema": ["Database"],
    "Table": ["Schema"],
    "Column": ["Table", "View"]
  },
  "publish_order": [
    ["Database"],
    ["Schema"],
    ["Table", "View"],
    ["Column"]
  ],
  "asset_type_assignments": [
    {
      "asset_type": "Database",
      "directory_prefix": "/workflow-id/run-id/diff_status=NEW/type_name=Database",
      "estimated_file_count": 5,
      "estimated_total_records": 10,
      "is_deletion": false,
      "processing_strategy": "type_level",
      "diff_status": "NEW"
    },
    {
      "asset_type": "Column",
      "directory_prefix": "/workflow-id/run-id/diff_status=DIFF/type_name=Column",
      "estimated_file_count": 500,
      "estimated_total_records": 50000,
      "is_deletion": false,
      "processing_strategy": "file_level",
      "diff_status": "DIFF",
      "files": [
        {
          "file_path": "/workflow-id/run-id/diff_status=DIFF/type_name=Column/1.parquet",
          "estimated_records": 200
        }
      ]
    },
    {
      "asset_type": "Table",
      "directory_prefix": "/workflow-id/run-id/diff_status=DELETE/type_name=Table",
      "estimated_file_count": 2,
      "estimated_total_records": 2,
      "is_deletion": true,
      "processing_strategy": "type_level",
      "diff_status": "DELETE"
    }
  ],
  "self_referential_types": ["Project"]
}
```
Please implement the planner phase components with proper error handling, following clean code principles and including comprehensive type hints.

