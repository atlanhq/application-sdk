# Application SDK Server API Documentation

This document provides comprehensive documentation for all APIs exposed by the Application SDK server, including their expected payload and response structures.

## Table of Contents

1. [Server Management APIs](#server-management-apis)
2. [Workflow Management APIs](#workflow-management-apis)
3. [Authentication & Metadata APIs](#authentication--metadata-apis)
4. [Configuration Management APIs](#configuration-management-apis)
5. [UI & Documentation APIs](#ui--documentation-apis)
6. [Error Handling](#error-handling)

---

## Server Management APIs

### Health Check
**Endpoint:** `GET /server/health`
**Description:** Check the health and system information of the server.

**Request:** No payload required.

**Response:**
```json
{
  "platform": "Darwin",
  "platform_release": "24.6.0",
  "platform_version": "Darwin Kernel Version 24.6.0",
  "architecture": "arm64",
  "hostname": "hostname.local",
  "ip_address": "192.168.1.100",
  "mac_address": "aa:bb:cc:dd:ee:ff",
  "processor": "arm",
  "ram": "16 GB"
}
```

### Readiness Check
**Endpoint:** `GET /server/ready`
**Description:** Check if the system is ready to accept requests.

**Request:** No payload required.

**Response:**
```json
{
  "status": "ok"
}
```

### Shutdown
**Endpoint:** `POST /server/shutdown`
**Description:** Gracefully shutdown the application server.

**Query Parameters:**
- `force` (boolean, *optional*): Whether to force immediate shutdown. Default: `false`

**Request:** No payload required.

**Response:**
```json
{
  "success": true,
  "message": "Application shutting down",
  "force": false
}
```

---

## Workflow Management APIs

### Start Workflow (HTTP Trigger)
**Endpoint:** `POST /workflows/v1/{custom_endpoint}`
**Description:** Start a workflow via HTTP trigger. The endpoint path is configurable based on registered workflow triggers (default: `/start`).

**Request Body:** (RootModel)
```json
{
  "workflow_id": "optional-custom-id",
  "argo_workflow_name": "optional-argo-name",
  "cron_schedule": "optional-cron-expression",
  "miner_args": {},
  "credentials": {
    "authType": "basic",
    "host": "localhost",
    "port": 5432,
    "username": "username",
    "password": "password",
    "database": "databasename"
  },
  "connection": {
    "connection": "dev"
  },
  "metadata": {
    "include-filter": "{\"^dbengine$\":[\"^public$\",\"^airflow$\"]}",
    "exclude-filter": "{}",
    "temp-table-regex": ""
  }
}
```

**Optional Fields:**
- `workflow_id` (string, *optional*): Custom workflow ID, otherwise auto-generated
- `argo_workflow_name` (string, *optional*): Used as workflow_id if workflow_id not provided
- `cron_schedule` (string, *optional*): Cron expression for scheduled workflows
- `credentials` (object, *optional*): Database credentials (stored separately as credential_guid)
  - **Supports `extra` field**: Additional sensitive parameters (see [Credential Field Format](#credential-field-format))
- All other fields are workflow-specific configuration

**Field Requirements:**
- **Required:** Root JSON object containing workflow configuration (flexible structure)
- **Data Flow Processing:**
  1. If `credentials` field exists:
     - `credential_guid = await SecretStore.save_secret(workflow_args["credentials"])`
     - **Development only**: Credentials stored in StateStore with type `CREDENTIALS`
     - **Production**: Throws `ValueError("Storing credentials is not supported in production")`
     - `workflow_args["credentials"]` is deleted and replaced with `credential_guid`
  2. If `workflow_id` not provided:
     - Generate new `workflow_id` (from `argo_workflow_name` or UUID)
     - Add `application_name` and `workflow_id` to workflow_args
     - Store complete config: `await StateStore.save_state_object(id=workflow_id, value=workflow_args, type=StateType.WORKFLOWS)`
  3. **Only `workflow_id` is passed to Temporal**: `args=[{"workflow_id": workflow_id}]`
- **Note:** The actual workflow receives only the `workflow_id` and retrieves full config from StateStore
- **Workflow Access Pattern:**
  - Workflow gets: `{"workflow_id": "generated-or-provided-id"}`
  - Workflow calls: `StateStore.get_state(workflow_id, StateType.WORKFLOWS)` to get full config
  - If credentials needed: `SecretStore.get_credentials(credential_guid)` to resolve credentials

**Response:**
```json
{
  "success": true,
  "message": "Workflow started successfully",
  "data": {
    "workflow_id": "4b805f36-48c5-4dd3-942f-650e06f75bbc",
    "run_id": "efe16ffe-24b2-4391-a7ec-7000c32c5893"
  }
}
```

**Response Field Requirements:**
- `success` (boolean, **required**): Indicates whether the operation was successful
- `message` (string, **required**): Message describing the result of the operation
- `data` (WorkflowData object, **required**): Workflow execution details
  - `workflow_id` (string, **required**): Unique identifier for the workflow
  - `run_id` (string, **required**): Unique identifier for the workflow run

**Error Response:** (On failure)
```json
{
  "success": false,
  "message": "Workflow failed to start",
  "data": {
    "workflow_id": "",
    "run_id": ""
  }
}
```

### Get Workflow Status
**Endpoint:** `GET /workflows/v1/status/{workflow_id}/{run_id}`
**Description:** Get the status of a specific workflow run.

**Path Parameters:**
- `workflow_id` (string, **required**): The unique identifier of the workflow
- `run_id` (string, **required**): The unique identifier of the workflow run

**Request:** No payload required.

**Response:**
```json
{
  "success": true,
  "message": "Workflow status fetched successfully",
  "data": {
    "status": "RUNNING",
    "start_time": "2024-01-01T12:00:00Z",
    "end_time": null,
    "result": null,
    "error": null
  }
}
```

**Response Field Requirements:**
- `success` (boolean, **required**): Always `true` for successful status retrieval
- `message` (string, **required**): Status retrieval confirmation message
- `data` (object, **required**): Workflow status information (structure depends on workflow client implementation)

**Note:** The `data` field contains the raw response from `workflow_client.get_workflow_run_status()` with `include_last_executed_run_id=True`

### Stop Workflow
**Endpoint:** `POST /workflows/v1/stop/{workflow_id}/{run_id}`
**Description:** Stop a running workflow.

**Path Parameters:**
- `workflow_id` (string, **required**): The unique identifier of the workflow
- `run_id` (string, **required**): The unique identifier of the workflow run

**Request:** No payload required.

**Response:**
```json
{
  "success": true
}
```

**Response Field Requirements:**
- `success` (boolean, **required**): Always `true` for successful stop request

---

## Authentication & Metadata APIs

### Test Authentication
**Endpoint:** `POST /workflows/v1/auth`
**Description:** Test database authentication credentials.

**Request Body:** (RootModel)
```json
{
  "authType": "basic",
  "host": "localhost",
  "port": 5432,
  "username": "username",
  "password": "password",
  "database": "databasename"
}
```

**Field Requirements:**
- **Required:** Root JSON object containing database credentials (flexible structure)
- **Supports `extra` field**: Additional sensitive parameters (see [Credential Field Format](#credential-field-format))
- **Note:** The payload is passed to `handler.load(body.model_dump())` then `handler.test_auth()` is called

**Response:**
```json
{
  "success": true,
  "message": "Authentication successful"
}
```

### Fetch Metadata
**Endpoint:** `POST /workflows/v1/metadata`
**Description:** Fetch database metadata (databases, schemas, tables, etc.).

**Request Body:** (RootModel)
```json
{
  "type": "all",
  "database": "specific_database",
  "authType": "basic",
  "host": "localhost",
  "port": 5432,
  "username": "username",
  "password": "password"
}
```

**Field Requirements:**
- **Required:** Root JSON object containing metadata request and credentials (flexible structure)
- **Supports `extra` field**: Additional sensitive parameters in credentials (see [Credential Field Format](#credential-field-format))
- **Data Flow:**
  1. `metadata_type = body.root.get("type", "all")` - extracts type with default
  2. `database = body.root.get("database", "")` - extracts database parameter
  3. Payload passed to `handler.load(body.model_dump())`
  4. `handler.fetch_metadata(metadata_type=metadata_type, database=database)` called
- Common fields:
  - `type` (string, *optional*): Metadata type filter - **enum values:** `"database"`, `"schema"`, `"all"` (default: `"all"`)
  - `database` (string, *optional*): Specific database to fetch metadata for (default: `""`)

**Response:**
```json
{
  "success": true,
  "data": {
    "databases": ["db1", "db2"],
    "schemas": {
      "db1": ["public", "private"],
      "db2": ["schema1", "schema2"]
    },
    "tables": {
      "db1.public": ["table1", "table2"],
      "db1.private": ["table3"]
    }
  }
}
```

### Preflight Check
**Endpoint:** `POST /workflows/v1/check`
**Description:** Perform preflight checks to validate configuration and connectivity.

**Request Body:**
```json
{
  "credentials": {
    "authType": "basic",
    "host": "localhost",
    "port": 5432,
    "username": "username",
    "password": "password",
    "database": "databasename"
  },
  "metadata": {
    "include-filter": "{\"^dbengine$\":[\"^public$\",\"^airflow$\"]}",
    "exclude-filter": "{}",
    "temp-table-regex": ""
  }
}
```

**Field Requirements:**
- `credentials` (object, **required**): Database credentials object
  - **Supports `extra` field**: Additional sensitive parameters (see [Credential Field Format](#credential-field-format))
- `metadata` (object, **required**): Form data for filtering and configuration

**Response:**
```json
{
  "success": true,
  "data": {
    "successMessage": "Successfully checked",
    "failureMessage": ""
  }
}
```

---

## Configuration Management APIs

### Get Workflow Configuration
**Endpoint:** `GET /workflows/v1/config/{config_id}`
**Description:** Retrieve workflow configuration by ID.

**Path Parameters:**
- `config_id` (string, **required**): The unique identifier of the configuration

**Query Parameters:**
- `type` (string, *optional*): Configuration type - **enum values:** `"workflows"`, `"credentials"` (default: `"workflows"`)

**Request:** No payload required.

**Response:**
```json
{
  "success": true,
  "message": "Workflow configuration fetched successfully",
  "data": {
    "credential_guid": "credential_test-uuid",
    "connection": {
      "connection": "dev"
    },
    "metadata": {
      "include-filter": "{\"^dbengine$\":[\"^public$\",\"^airflow$\"]}",
      "exclude-filter": "{}",
      "temp-table-regex": ""
    }
  }
}
```

### Update Workflow Configuration
**Endpoint:** `POST /workflows/v1/config/{config_id}`
**Description:** Update workflow configuration.

**Path Parameters:**
- `config_id` (string, **required**): The unique identifier of the configuration

**Query Parameters:**
- `type` (string, *optional*): Configuration type - **enum values:** `"workflows"`, `"credentials"` (default: `"workflows"`)

**Request Body:** (RootModel)
```json
{
  "credential_guid": "credential_test-uuid",
  "connection": {
    "connection": "prod"
  },
  "metadata": {
    "include-filter": "{\"^dbengine$\":[\"^public$\"]}",
    "exclude-filter": "{}",
    "temp-table-regex": ""
  }
}
```

**Field Requirements:**
- **Required:** Root JSON object containing workflow configuration (flexible structure)

**Response:**
```json
{
  "success": true,
  "message": "Workflow configuration updated successfully",
  "data": {
    "credential_guid": "credential_test-uuid",
    "connection": {
      "connection": "prod"
    },
    "metadata": {
      "include-filter": "{\"^dbengine$\":[\"^public$\"]}",
      "exclude-filter": "{}",
      "temp-table-regex": ""
    }
  }
}
```

### Get Configuration Map
**Endpoint:** `GET /workflows/v1/configmap/{config_map_id}`
**Description:** Get a configuration map by its ID, general used for Atlan UI display.

**Path Parameters:**
- `config_map_id` (string, **required**): The unique identifier of the configuration map

**Request:** No payload required.

**Response:**
```json
{
  "success": true,
  "message": "Configuration map fetched successfully",
  "data": {
    "config_map_id": "pikachu-config-001",
    "name": "Pikachu Configuration",
    "settings": {
      "electric_type": true,
      "level": 25,
      "moves": ["Thunderbolt", "Quick Attack"]
    }
  }
}
```

---

## UI & Documentation APIs

### Home Page
**Endpoint:** `GET /`
**Description:** Serve the main application UI homepage.

**Request:** No payload required.

**Response:** HTML content of the application dashboard.

### Observability Dashboard
**Endpoint:** `GET /observability`
**Description:** Redirect to DuckDB UI for log exploration.

**Request:** No payload required.

**Response:** HTTP 302 redirect to `http://0.0.0.0:4213`

### Documentation
**Endpoint:** `GET /atlandocs/*`
**Description:** Serve static documentation files generated by AtlanDocsGenerator.

**Request:** No payload required.

**Response:** Static HTML/CSS/JS files for documentation.

---

## Error Handling

All API endpoints use a standardized error response format:

### Standard Error Response
```json
{
  "success": false,
  "error": "An internal error has occurred.",
  "details": "Specific error message or exception details"
}
```

### HTTP Status Codes
- `200 OK`: Successful operation
- `400 Bad Request`: Invalid request payload or parameters
- `401 Unauthorized`: Authentication failed
- `404 Not Found`: Resource not found
- `500 Internal Server Error`: Server-side error

---

## Field Requirements Summary

### Required vs Optional Field Notation
- **Required fields** are marked with `**required**` and use the `...` (ellipsis) in Pydantic Field definitions
- **Optional fields** are marked with `*optional*` and have default values or are not required
- **Enum fields** show all possible values with `**enum values:**` followed by the allowed values

### Common Field Types
- **RootModel fields**: Accept a defined JSON object structure (flexible schema)
- **Typed fields**: Have specific data types (string, boolean, object, array)
- **Aliased fields**: Use different names in JSON (e.g., `alias="data"`)

### Enum Values Reference
- **MetadataType**: `"database"`, `"schema"`, `"all"`
- **StateType**: `"workflows"`, `"credentials"`
- **WorkflowStates**: `"unknown"`, `"running"`, `"completed"`, `"failed"`
- **ApplicationEventNames**: Various application-specific event names

### Pydantic Model Patterns
- **BaseModel**: Structured models with typed fields
- **RootModel**: Flexible models accepting a defined JSON object structure
- **Field aliases**: JSON field names may differ from model field names
- **Default values**: Optional fields often have sensible defaults

### Workflow Trigger Configuration
- **HttpWorkflowTrigger**:
  - `endpoint` (string, *optional*): Default `"/start"`
  - `methods` (List[string], *optional*): Default `["POST"]`
  - `workflow_class` (Type[WorkflowInterface], *optional*): Set during registration

### Credential Field Format

The Application SDK uses a standardized credential structure with support for an `extra` field for additional sensitive parameters:

**Required Fields**: Credentials requirements are defined by each database client's `DB_CONFIG.required` list. Common patterns include:
- `username` (string, **required**): Database username
- `password` (string, **required**): Database password (resolved via `get_auth_token()` based on `authType`)
- Database-specific fields: `host`, `port`, `database`, `account_id`, etc. (as defined in `DB_CONFIG.required`)

**Extra Field**: `extra` (object, *optional*) - Contains additional sensitive parameters
- **Purpose**: Store sensitive data beyond standard credentials such as SSL certificates, API keys, OAuth tokens, private keys, and custom authentication parameters
- **Format**: Can be provided as JSON string or object (parsed automatically using `parse_credentials_extra()` utility)
- **Validation**: Fields in `extra` can satisfy `DB_CONFIG.required` parameters if not found in root credentials
- **Secret Resolution**: Both root-level credential fields and `extra` fields support secret reference substitution via the `apply_secret_values()` method

**Validation Logic**:
```python
# For each required parameter in DB_CONFIG.required:
value = credentials.get(param) or extra.get(param)
if value is None:
    raise ValueError(f"{param} is required")
```

**Example**:
```json
{
  "username": "user",
  "password": "password",
  "host": "localhost",
  "port": 5432,
  "database": "mydb",
  "extra": {
    "ssl_cert": "-----BEGIN CERTIFICATE-----...",
    "api_key": "sk-1234567890abcdef",
    "oauth_token": "bearer_token_123",
    "ssl_mode": "require"
  }
}
```

This documentation covers all the APIs currently exposed by the Application SDK server. Each endpoint includes comprehensive request/response examples, field requirements (required/optional), enum values, and describes the expected behavior and error conditions.
