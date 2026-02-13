"""Router for handling credential-related API endpoints.

This module provides endpoints for:
- Getting credential configmap schema from handler declarations
- Validating credential configurations

These endpoints are used by the frontend to:
- Render credential input forms
- Configure credential profiles with proper field mappings
"""

from typing import Any, Dict, Optional, Type

from fastapi import Query, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter

from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

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
        logger.error(f"Failed to get credentials configmap: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Internal server error",
                "message": str(e),
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
            logger.error(f"Failed to get configmap for handler '{handler_id}': {e}")
            result["handlers"][handler_id] = {
                "error": str(e),
            }

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=result,
    )


def get_credentials_router() -> APIRouter:
    """Get the credentials router.

    Returns:
        The FastAPI router with credential endpoints.
    """
    return router
