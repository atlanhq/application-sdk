"""Router for handling credential-related API endpoints.

This module provides endpoints for:
- Getting credential configmap schema from handler declarations
- Storing credentials for local development (simulates Vault)
- Validating credential configurations

These endpoints are used by the frontend (Credential-v2 widget) to:
- Render credential input forms from declare_credentials()
- Store credentials locally before workflow start
- Configure credential profiles with proper field mappings

Local Development Flow:
    1. App-playground fetches configmap from GET /credentials/configmap
    2. User fills Credential-v2 widget
    3. App-playground saves credentials via POST /credentials/input
    4. App-playground starts workflow with credential_guid
    5. SDK reads credentials from local secrets file via Dapr
"""

from typing import Any, Dict, Optional, Type

from fastapi import Query, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter

from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT
from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Local credential storage for simulating production Vault
# Maps credential_guid -> slot_name for local development
_local_credential_slots: Dict[str, str] = {}

# Create a router instance
router = APIRouter(
    prefix="/credentials",
    tags=["credentials"],
    responses={404: {"description": "Not found"}},
)

# Registry of handler classes for configmap lookup
_handler_registry: Dict[str, Type[HandlerInterface]] = {}


def register_handler(handler_id: str, handler_class: Type[HandlerInterface]) -> None:
    """Register a handler class for configmap lookup.

    Args:
        handler_id: Unique identifier for the handler (e.g., app ID).
        handler_class: The handler class that implements HandlerInterface.
    """
    _handler_registry[handler_id] = handler_class
    logger.debug(f"Registered handler '{handler_id}' for configmap lookup")


def get_registered_handler(handler_id: str) -> Optional[Type[HandlerInterface]]:
    """Get a registered handler class by ID.

    Args:
        handler_id: The handler identifier.

    Returns:
        The handler class or None if not found.
    """
    return _handler_registry.get(handler_id)


@router.get("/configmap")
async def get_credentials_configmap(
    handler_id: Optional[str] = Query(None, description="Handler/App ID"),
) -> JSONResponse:
    """Get the credential configmap schema for a handler.

    This endpoint returns the credential requirements declared by a handler,
    including:
    - Credential slot names
    - Authentication modes and protocols
    - Field specifications (name, label, type, required, sensitive)

    The schema is used by the frontend to:
    - Render credential input forms
    - Configure credential profiles with proper field mappings
    - Validate that all required credentials are provided

    Args:
        handler_id: Optional handler/app ID. If not provided, returns
            configmap for the default registered handler.

    Returns:
        JSON response with credential configmap schema.

    Example response:
        {
            "handler_name": "MyHandler",
            "credentials": [
                {
                    "name": "api",
                    "auth_mode": "api_key",
                    "protocol_type": "static_secret",
                    "required": true,
                    "fields": [
                        {
                            "name": "api_key",
                            "label": "API Key",
                            "field_type": "password",
                            "required": true,
                            "sensitive": true
                        }
                    ]
                }
            ]
        }
    """
    # If no handler_id provided, try to get the default handler
    if handler_id:
        handler_class = get_registered_handler(handler_id)
    elif _handler_registry:
        # Return the first registered handler if no ID specified
        handler_id = next(iter(_handler_registry.keys()))
        handler_class = _handler_registry[handler_id]
    else:
        handler_class = None

    if not handler_class:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "Handler not found",
                "message": f"No handler registered with ID '{handler_id}'"
                if handler_id
                else "No handlers registered",
            },
        )

    try:
        configmap = handler_class.get_credentials_configmap()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=configmap,
        )
    except Exception as e:
        logger.error(f"Failed to get credentials configmap: {e}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Internal server error",
                "message": "Failed to generate credentials configmap. Check server logs for details.",
            },
        )


@router.get("/configmap/all")
async def get_all_credentials_configmaps() -> JSONResponse:
    """Get credential configmap schemas for all registered handlers.

    Returns:
        JSON response with configmaps for all handlers.

    Example response:
        {
            "handlers": {
                "handler_1": { ... configmap ... },
                "handler_2": { ... configmap ... }
            }
        }
    """
    result: Dict[str, Any] = {"handlers": {}}

    for handler_id, handler_class in _handler_registry.items():
        try:
            configmap = handler_class.get_credentials_configmap()
            result["handlers"][handler_id] = configmap
        except Exception as e:
            logger.error(
                f"Failed to get configmap for handler '{handler_id}': {e}",
                exc_info=True,
            )
            result["handlers"][handler_id] = {
                "error": "Failed to generate configmap. Check server logs for details.",
            }

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=result,
    )


@router.post("/input")
async def store_credential_input(
    payload: Dict[str, Any],
    slot_name: Optional[str] = Query(None, description="Credential slot name"),
) -> JSONResponse:
    """Store credentials for local development (simulates Vault storage).

    This endpoint is used by the Credential-v2 widget in app-playground to
    store credentials before starting a workflow. It simulates the production
    flow where credentials are stored in Vault during Phase 1.

    Local development only - returns 403 in production.

    Args:
        payload: Credential data to store (e.g., {"base_url": "...", "api_key": "..."})
        slot_name: Optional credential slot name (from declare_credentials).
            If not provided, uses the first declared slot.

    Returns:
        JSON response with the credential_guid to use in workflow start.

    Example:
        POST /credentials/input?slot_name=atlan
        Body: {"base_url": "https://mycompany.atlan.com", "api_key": "my-api-key"}

        Response:
        {
            "credential_guid": "abc-123-def-456",
            "slot_name": "atlan",
            "message": "Credentials stored successfully"
        }

    Usage in workflow start:
        POST /workflows/v1/start
        Body: {"credential_guid": "abc-123-def-456", ...}
    """
    if DEPLOYMENT_NAME != LOCAL_ENVIRONMENT:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": "Not allowed",
                "message": "Credential input is only available in local development mode. "
                "In production, credentials are managed via Heracles/Vault.",
            },
        )

    try:
        from application_sdk.services.secretstore import SecretStore

        # Determine slot name from handler declarations if not provided
        if not slot_name and _handler_registry:
            handler_class = next(iter(_handler_registry.values()))
            if hasattr(handler_class, "declare_credentials"):
                declarations = handler_class.declare_credentials()
                if declarations:
                    slot_name = declarations[0].name

        if not slot_name:
            slot_name = "default"

        # Save credentials to local secrets file
        credential_guid = await SecretStore.save_secret(payload)

        # Track slot mapping for this credential
        _local_credential_slots[credential_guid] = slot_name

        logger.info(
            f"Stored credentials for slot '{slot_name}' with GUID: {credential_guid}"
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "credential_guid": credential_guid,
                "slot_name": slot_name,
                "message": "Credentials stored successfully. Use credential_guid in workflow start.",
            },
        )

    except Exception as e:
        logger.error(f"Failed to store credentials: {e}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Failed to store credentials",
                "message": "An error occurred while storing credentials. Check server logs for details.",
            },
        )


def get_credential_slot_mapping(credential_guid: str) -> Optional[str]:
    """Get the slot name for a stored credential GUID.

    Args:
        credential_guid: The credential GUID from /credentials/input.

    Returns:
        The slot name or None if not found.
    """
    return _local_credential_slots.get(credential_guid)


def get_credentials_router() -> APIRouter:
    """Get the credentials router.

    Returns:
        The FastAPI router with credential endpoints.
    """
    return router
