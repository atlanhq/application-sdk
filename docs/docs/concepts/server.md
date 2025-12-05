# Server

This module provides the core server framework for building Atlan applications, particularly focusing on integrating with FastAPI to expose web endpoints for interacting with workflows and handlers.

## Core Concepts

1.  **`ServerInterface` (`application_sdk.server.__init__.py`)**:
    *   **Purpose:** The abstract base class for all server types within the SDK. It defines a minimal interface, primarily requiring a `start()` method and optionally accepting a `HandlerInterface` instance.
    *   **Extensibility:** Subclasses must implement the `start()` method to define how the server initializes and begins running (e.g., starting a web server).

2.  **`APIServer` (`application_sdk.server.fastapi.__init__.py`)**:
    *   **Purpose:** A concrete implementation of `ServerInterface` built on top of the FastAPI web framework. It provides a ready-to-use web server setup with pre-configured endpoints for common operations like health checks, authentication testing, metadata fetching, workflow management, and documentation serving.
    *   **Components:**
        *   Integrates with `HandlerInterface` subclasses to perform backend operations.
        *   Integrates with `WorkflowClient` and `WorkflowInterface` subclasses to manage and trigger Temporal workflows.
        *   Uses FastAPI's `APIRouter` to organize endpoints.
        *   Includes default middleware (`LogMiddleware`).
        *   Sets up documentation generation and serving using `AtlanDocsGenerator`.
        *   Supports UI templates through `Jinja2Templates`.

3.  **Routers (`application_sdk.server.fastapi.routers/`)**:
    *   **Purpose:** Organize related API endpoints. The SDK provides a default `server` router (`server.py`) with endpoints like `/health`, `/ready`, and `/shutdown`.
    *   **Extensibility:** Developers can add custom routers to group their application-specific endpoints.

4.  **Workflow Triggers (`application_sdk.server.fastapi.__init__.py`)**:
    *   **Purpose:** Define how workflows are initiated.
        *   `HttpWorkflowTrigger`: Triggers a workflow via an HTTP request to a specific endpoint registered via `register_workflow`. Requires `WorkflowClient` to be configured.
        *   `EventWorkflowTrigger`: Triggers a workflow based on incoming events

5.  **Models (`application_sdk.server.fastapi.models.py`)**:
    *   **Purpose:** Defines Pydantic models used for request/response validation and serialization for the default API endpoints (e.g., `TestAuthRequest`, `WorkflowResponse`, `PreflightCheckRequest`, `PreflightCheckResponse`).

## Usage Patterns

### 1. Using the Default FastAPI Server

For standard use cases where you only need the built-in endpoints to interact with your custom handler and trigger workflows via HTTP, you can instantiate the base `APIServer` class directly.

```python
# In your main server file (e.g., main.py)
import asyncio
from application_sdk.server.fastapi import APIServer
from application_sdk.clients.workflow import WorkflowClient
from application_sdk.constants import APPLICATION_NAME
# Assuming your custom classes are defined in a package 'my_connector'
from my_connector.handlers import MyConnectorHandler
from my_connector.workflows import MyConnectorWorkflow

# Instantiate the base Server with your handler
api_server = APIServer(
    handler=MyConnectorHandler(),
    workflow_client=WorkflowClient(application_name=APPLICATION_NAME)
)

# Register your workflow(s) with HTTP triggers
api_server.register_workflow(
    workflow_class=MyConnectorWorkflow,
    triggers=[
        HttpWorkflowTrigger(
            endpoint="/start-extraction",
            methods=["POST"],
        )
    ],
)

async def main():
    # Start the FastAPI server
    await api_server.start()

if __name__ == "__main__":
    asyncio.run(main())
```

This setup provides:
*   Endpoints defined in `application_sdk.server.fastapi.routers.server` (e.g., `/server/health`)
*   Endpoints for interacting with the `handler` (e.g., `/workflows/v1/test_auth`, `/workflows/v1/metadata`, `/workflows/v1/preflight_check`)
*   The endpoint(s) you defined via `HttpWorkflowTrigger` (e.g., `/workflows/v1/start-extraction`)
*   Documentation served at `/atlandocs`

### 2. Adding Custom Endpoints & Triggering Workflows

If you need server-specific API endpoints, potentially with custom logic before triggering a workflow, create a new class inheriting from `APIServer` and add your own `APIRouter`.

```python
# In your main server file (e.g., main.py)
import asyncio
import uuid
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from application_sdk.server.fastapi import APIServer
from application_sdk.clients.workflow import WorkflowClient
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.constants import APPLICATION_NAME
# Assuming your custom classes are defined elsewhere
from my_connector.handlers import MyConnectorHandler
from my_connector.workflows import MyConnectorWorkflow

logger = get_logger(__name__)

# Define Request Model for the Custom Endpoint
class CustomProcessingRequest(BaseModel):
    source_system_id: str
    target_dataset_name: str
    processing_mode: str = "delta"
    api_key_secret_ref: str

# Define your custom server class
class MyCustomServer(APIServer):
    custom_router: APIRouter = APIRouter()

    def register_routers(self):
        # Include the custom router BEFORE calling super()
        self.app.include_router(self.custom_router, prefix="/custom-api", tags=["custom-processing"])
        # Call super() to include default routers
        super().register_routers()

    def register_routes(self):
        self.custom_router.add_api_route(
            "/trigger",
            self.handle_custom_trigger,
            methods=["POST"],
            summary="Triggers a tailored processing workflow",
            status_code=status.HTTP_202_ACCEPTED
        )
        # Call super() to register default routes
        super().register_routes()

    async def handle_custom_trigger(self, request_body: CustomProcessingRequest) -> dict:
        logger.info(f"Received request to process from {request_body.source_system_id} to {request_body.target_dataset_name}")
        if request_body.processing_mode not in ["delta", "full"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid processing_mode.")

        if not self.workflow_client:
            logger.error("Workflow client not initialized.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Workflow service is not configured.",
            )

        workflow_args = {
            "credentials": { "apiKey": request_body.api_key_secret_ref },
            "connection": {
                "source_id": request_body.source_system_id,
                "target_name": request_body.target_dataset_name,
            },
            "parameters": {
                 "mode": request_body.processing_mode,
            },
            "tenant_id": "your-tenant-id",
            "workflow_id": f"custom-proc-{request_body.source_system_id}-{uuid.uuid4()}"
        }

        try:
            workflow_data = await self.workflow_client.start_workflow(
                workflow_args=workflow_args,
                workflow_class=MyConnectorWorkflow
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

# Instantiate and run
api_server = MyCustomServer(
    handler=MyConnectorHandler(),
    workflow_client=WorkflowClient(application_name=APPLICATION_NAME)
)

async def main():
    await api_server.start()

if __name__ == "__main__":
    asyncio.run(main())
```

### 3. Overriding Standard Endpoint Behavior

The `APIServer` class provides default endpoints like `/workflows/v1/test_auth`, `/workflows/v1/metadata`, and `/workflows/v1/preflight_check`. The logic executed by these endpoints is determined by the corresponding methods defined on the handler instance passed to the `APIServer` constructor.

```python
# In your handler file (e.g., my_connector/handlers.py)
from typing import Any, Dict, List, Optional
from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server.fastapi.models import MetadataType

logger = get_logger(__name__)

class MyConnectorHandler(HandlerInterface):
    async def load(self, **kwargs: Any) -> None:
        logger.info("MyConnectorHandler loading...")
        pass

    async def test_auth(self, **kwargs: Any) -> bool:
        logger.info("Running custom test_auth logic...")
        credentials = kwargs.get("credentials", {})
        api_key = credentials.get("api_key")
        if api_key and len(api_key) > 10:
             logger.info("Custom auth successful.")
             return True
        else:
             logger.warning("Custom auth failed.")
             return False

    async def fetch_metadata(
        self,
        metadata_type: Optional[MetadataType] = None,
        database: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, str]]:
        logger.info(f"Running custom fetch_metadata for type: {metadata_type}, database: {database}")
        if metadata_type == MetadataType.DATABASE:
             return [{"database_name": "prod_db"}, {"database_name": "staging_db"}]
        elif metadata_type == MetadataType.SCHEMA and database == "prod_db":
             return [{"schema_name": "analytics"}, {"schema_name": "reporting"}]
        else:
            return []

    async def preflight_check(
        self, payload: Dict[str, Any], **kwargs: Any
    ) -> Dict[str, Any]:
        logger.info("Running custom preflight_check...")
        connectivity_ok = True
        perms_ok = True

        return {
            "success": connectivity_ok and perms_ok,
            "data": {
                "connectivityCheck": {"success": connectivity_ok, "message": "System reachable" if connectivity_ok else "System unreachable"},
                "permissionsCheck": {"success": perms_ok, "message": "Required permissions verified" if perms_ok else "Permissions missing"},
            },
            "message": "Custom preflight checks completed."
        }

# In your main server file:
from my_connector.handlers import MyConnectorHandler

api_server = APIServer(
    handler=MyConnectorHandler(),
    workflow_client=WorkflowClient(application_name=APPLICATION_NAME)
)
```

## Summary

The `application_sdk.server` module, especially the `fastapi` sub-package, provides a robust foundation for building web servers that interact with Atlan handlers and Temporal workflows. You can use the default `APIServer` for simple cases, extend it with custom routers for specific API needs, and override handler methods to tailor the behavior of standard API endpoints.