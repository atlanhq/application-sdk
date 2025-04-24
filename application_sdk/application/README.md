# Application SDK - Application

This module provides the core framework for building Atlan applications, particularly focusing on integrating with FastAPI to expose web endpoints for interacting with workflows and handlers.

## Core Concepts

1.  **`AtlanApplicationInterface` (`application_sdk.application.__init__.py`)**:
    *   **Purpose:** The abstract base class for all application types within the SDK. It defines a minimal interface, primarily requiring a `start()` method and optionally accepting a `HandlerInterface` instance.
    *   **Extensibility:** Subclasses must implement the `start()` method to define how the application initializes and begins running (e.g., starting a web server).

2.  **`Application` (`application_sdk.application.fastapi.__init__.py`)**:
    *   **Purpose:** A concrete implementation of `AtlanApplicationInterface` built on top of the FastAPI web framework. It provides a ready-to-use web server setup with pre-configured endpoints for common operations like health checks, authentication testing, metadata fetching, workflow management, and documentation serving.
    *   **Components:**
        *   Integrates with `HandlerInterface` subclasses to perform backend operations.
        *   Integrates with `WorkflowClient` and `WorkflowInterface` subclasses to manage and trigger Temporal workflows.
        *   Uses FastAPI's `APIRouter` to organize endpoints.
        *   Includes default middleware (`LogMiddleware`).
        *   Sets up documentation generation and serving using `AtlanDocsGenerator`.

3.  **Routers (`application_sdk.application.fastapi.routers/`)**:
    *   **Purpose:** Organize related API endpoints. The SDK provides a default `server` router (`server.py`) with endpoints like `/health`, `/ready`, and `/shutdown`.
    *   **Extensibility:** Developers can add custom routers to group their application-specific endpoints.

4.  **Workflow Triggers (`application_sdk.application.fastapi.__init__.py`)**:
    *   **Purpose:** Define how workflows are initiated.
        *   `HttpWorkflowTrigger`: Triggers a workflow via an HTTP request to a specific endpoint registered via `register_workflow`. Requires `WorkflowClient` to be configured.
        *   `EventWorkflowTrigger`: (Less common example shown) Triggers a workflow based on incoming events (e.g., from a message queue), evaluated by a `should_trigger_workflow` function.

5.  **Models (`application_sdk.application.fastapi.models.py`)**:
    *   **Purpose:** Defines Pydantic models used for request/response validation and serialization for the default API endpoints (e.g., `TestAuthRequest`, `WorkflowResponse`).

## Usage Patterns

### 1. Using the Default FastAPI Application

For standard use cases where you only need the built-in endpoints to interact with your custom handler and trigger workflows via HTTP, you can instantiate the base `Application` class directly.

```python
# In your main application file (e.g., main.py)
import asyncio
# Absolute imports
from application_sdk.application.fastapi import Application, HttpWorkflowTrigger
from application_sdk.clients.utils import get_workflow_client # Example utility
# Assuming your custom classes are defined in a package 'my_connector'
from my_connector.handlers import MyConnectorHandler # Your handler implementation
from my_connector.workflows import MyConnectorWorkflow # Your workflow implementation

# Instantiate the base Application with your handler
fast_api_app = Application(
    handler=MyConnectorHandler(),
    # Provide a workflow_client if using HTTP triggers
    workflow_client=get_workflow_client(application_name="my-connector")
)

# Register your workflow(s) with HTTP triggers
fast_api_app.register_workflow(
    MyConnectorWorkflow, # The workflow class itself
    [
        HttpWorkflowTrigger(
            endpoint="/start-extraction", # Custom endpoint path (relative to /workflows/v1)
            methods=["POST"],
            # The workflow class is implicitly linked here by register_workflow
        )
    ],
)

async def main():
    # Start the FastAPI server
    await fast_api_app.start()

if __name__ == "__main__":
    asyncio.run(main())
```

This setup provides:
*   Endpoints defined in `application_sdk.application.fastapi.routers.server.py` (e.g., `/server/health`).
*   Endpoints for interacting with the `handler` (e.g., `/workflows/v1/test_auth`, `/workflows/v1/metadata`, `/workflows/v1/preflight_check`).
*   The endpoint(s) you defined via `HttpWorkflowTrigger` (e.g., `/workflows/v1/start-extraction`).
*   Documentation served at `/atlandocs`.

### 2. Adding Custom Endpoints & Triggering Workflows

If you need application-specific API endpoints, potentially with custom logic before triggering a workflow, create a new class inheriting from `Application` and add your own `APIRouter`. You can manually trigger workflows using `self.workflow_client`.

```python
# In your main application file (e.g., main.py)
import asyncio
import uuid # For generating unique IDs if needed
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

# Absolute imports
from application_sdk.application.fastapi import Application
from application_sdk.clients.utils import get_workflow_client # Example utility
from application_sdk.common.logger_adaptors import get_logger
# Assuming your custom classes are defined elsewhere
from my_connector.handlers import MyConnectorHandler
from my_connector.workflows import MyConnectorWorkflow

logger = get_logger(__name__)

# --- Define Request Model for the Custom Endpoint ---
class CustomProcessingRequest(BaseModel):
    source_system_id: str
    target_dataset_name: str
    processing_mode: str = "delta"
    api_key_secret_ref: str # Reference to fetch the actual key

# --- Define your custom application class ---
class MyCustomApplication(Application):
    custom_router: APIRouter = APIRouter()

    def register_routers(self):
        # Include the custom router BEFORE calling super() if you want its prefix
        self.app.include_router(self.custom_router, prefix="/custom-api", tags=["custom-processing"])
        # IMPORTANT: Call super() to include default routers AFTER your custom one
        super().register_routers()

    def register_routes(self):
        self.custom_router.add_api_route(
            "/trigger",
            self.handle_custom_trigger, # Method to handle the request
            methods=["POST"],
            summary="Triggers a tailored processing workflow",
            status_code=status.HTTP_202_ACCEPTED # Good practice for async starts
        )
        # IMPORTANT: Call super() to register default routes
        super().register_routes()

    # --- Handler method for the custom route ---
    async def handle_custom_trigger(self, request_body: CustomProcessingRequest) -> dict:
        # 1. Custom Logic/Validation
        logger.info(f"Received request to process from {request_body.source_system_id} to {request_body.target_dataset_name}")
        if request_body.processing_mode not in ["delta", "full"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid processing_mode.")

        # 2. Check if workflow client is available
        if not self.workflow_client:
            logger.error("Workflow client not initialized. Cannot start workflow.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Workflow service is not configured or available.",
            )

        # 3. Prepare Arguments for the Workflow
        # Fetch sensitive data securely if needed (example only)
        # actual_api_key = await fetch_secret(request_body.api_key_secret_ref)

        workflow_args = {
            "credentials": { "apiKey": "fetched-secret-value" }, # Use fetched secret
            "connection": {
                "source_id": request_body.source_system_id,
                "target_name": request_body.target_dataset_name,
            },
            "parameters": {
                 "mode": request_body.processing_mode,
            },
            "tenant_id": "your-tenant-id", # Or get dynamically
            "workflow_id": f"custom-proc-{request_body.source_system_id}-{uuid.uuid4()}" # Example unique ID
        }

        # 4. Start the Workflow via the Workflow Client
        try:
            # Ensure MyConnectorWorkflow is imported and correct
            workflow_data = await self.workflow_client.start_workflow(
                workflow_args=workflow_args,
                workflow_class=MyConnectorWorkflow # The specific workflow class
            )
            return {
                "message": "Custom processing workflow initiated successfully.",
                "workflow_id": workflow_data.get("workflow_id"),
                "run_id": workflow_data.get("run_id"),
            }
        except Exception as e:
             logger.error(f"Failed to start workflow: {e}", exc_info=True)
             raise HTTPException(
                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                 detail=f"Failed to initiate workflow execution: {e}",
             )

# --- Instantiate and run ---
fast_api_app = MyCustomApplication(
    handler=MyConnectorHandler(),
    # Make sure workflow_client is initialized
    workflow_client=get_workflow_client(application_name="my-connector")
)

# Note: No need for register_workflow for the '/custom-api/trigger' route,
# as its logic is handled directly within handle_custom_trigger.

async def main():
    await fast_api_app.start()

if __name__ == "__main__":
    asyncio.run(main())
```
This adds your custom endpoint (e.g., `/custom-api/trigger`) alongside the default ones. The custom route handler (`handle_custom_trigger`) contains the logic to prepare arguments and uses `self.workflow_client.start_workflow` to initiate the desired workflow.

### 3. Overriding Standard Endpoint Behavior (`/test_auth`, `/metadata`, `/preflight_check`)

The `Application` class provides default endpoints like `/workflows/v1/test_auth`, `/workflows/v1/metadata`, and `/workflows/v1/preflight_check`. The *logic* executed by these endpoints is determined by the corresponding methods (`test_auth`, `fetch_metadata`, `preflight_check`) defined on the **handler instance** passed to the `Application` constructor.

Therefore, to change what happens when these endpoints are called, you do **not** override methods in your `Application` subclass. Instead, you create a custom handler class inheriting from the appropriate base handler (e.g., `HandlerInterface`, `SQLHandler`) and override the specific methods there.

```python
# In your handler file (e.g., my_connector/handlers.py)
from typing import Any, Dict, List, Optional
# Absolute imports
from application_sdk.handlers import HandlerInterface # Or SQLHandler etc.
from application_sdk.common.logger_adaptors import get_logger
# Import specific request/response models if needed for type hinting or logic
from application_sdk.application.fastapi.models import MetadataType

logger = get_logger(__name__)

class MyConnectorHandler(HandlerInterface): # Or inherit from SQLHandler, etc.

    async def load(self, **kwargs: Any) -> None:
        # Custom initialization logic for your handler
        logger.info("MyConnectorHandler loading...")
        # Example: Initialize a specific client based on credentials
        # self.connector_client = SpecificAPIClient()
        # await self.connector_client.connect(**kwargs.get("credentials", {}))
        pass

    # Override test_auth to implement custom authentication logic
    async def test_auth(self, **kwargs: Any) -> bool:
        logger.info("Running custom test_auth logic...")
        credentials = kwargs.get("credentials", {})
        # Implement your logic to validate credentials against the target system
        # Example: return await self.connector_client.validate_token(credentials.get("token"))
        api_key = credentials.get("api_key")
        if api_key and len(api_key) > 10:
             logger.info("Custom auth successful (basic key check).")
             return True
        else:
             logger.warning("Custom auth failed (invalid API key).")
             # You might raise specific exceptions for detailed error reporting
             # raise PermissionError("Invalid or missing API Key")
             return False

    # Override fetch_metadata for custom metadata retrieval
    async def fetch_metadata(
        self,
        metadata_type: Optional[MetadataType] = None,
        database: Optional[str] = None,
        # Add any other specific parameters your fetch logic needs
        **kwargs: Any,
    ) -> List[Dict[str, str]]: # Adjust return type if necessary
        logger.info(f"Running custom fetch_metadata for type: {metadata_type}, database: {database}")
        # Implement logic to fetch metadata from your source system using self.connector_client
        # Example: return await self.connector_client.list_objects(type=metadata_type, container=database)
        # Dummy implementation:
        if metadata_type == MetadataType.DATABASE:
             return [{"database_name": "prod_db"}, {"database_name": "staging_db"}]
        elif metadata_type == MetadataType.SCHEMA and database == "prod_db":
             return [{"schema_name": "analytics"}, {"schema_name": "reporting"}]
        else:
            return [] # Return empty list if no match or not implemented

    # Override preflight_check for custom validation logic
    async def preflight_check(
        self, payload: Dict[str, Any], **kwargs: Any
    ) -> Dict[str, Any]: # Adjust return type according to PreflightCheckResponse structure
        logger.info("Running custom preflight_check...")
        # Implement checks against the target system based on payload (e.g., connection config)
        # Example: check connectivity, permissions for specific operations
        # connectivity_ok = await self.connector_client.ping()
        # write_perms_ok = await self.connector_client.check_write_access(payload.get("target_location"))
        connectivity_ok = True # Dummy value
        perms_ok = True      # Dummy value

        return {
            "success": connectivity_ok and perms_ok, # Overall success depends on all checks
            "data": { # Structure matches PreflightCheckResponse.data
                "connectivityCheck": {"success": connectivity_ok, "message": "System reachable" if connectivity_ok else "System unreachable"},
                "permissionsCheck": {"success": perms_ok, "message": "Required permissions verified" if perms_ok else "Permissions missing"},
                # Add results of your other custom checks here
            },
            "message": "Custom preflight checks completed." # Optional overall message
        }

# In your main application file (e.g., main.py):
# ... imports ...
from my_connector.handlers import MyConnectorHandler # Import your custom handler

# Instantiate Application with YOUR custom handler
fast_api_app = Application(
    handler=MyConnectorHandler(),
    # ... other setup (workflow_client, etc.) ...
)

# Now, when requests hit /workflows/v1/test_auth, /workflows/v1/metadata, etc.,
# the overridden methods in MyConnectorHandler will be executed.
```
By providing an instance of `MyConnectorHandler` to the `Application`, the default endpoints automatically route to your custom implementations defined within that handler.

## Summary

The `application_sdk.application` module, especially the `fastapi` sub-package, provides a robust foundation for building web applications that interact with Atlan handlers and Temporal workflows. You can use the default `Application` for simple cases, extend it with custom routers for specific API needs, and override handler methods to tailor the behavior of standard API endpoints.