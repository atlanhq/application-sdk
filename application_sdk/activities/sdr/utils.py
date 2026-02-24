"""Utility functions for SDR activities."""

from typing import Any, Dict, Type

from application_sdk.clients import ClientInterface
from application_sdk.handlers import HandlerInterface
from application_sdk.services.secretstore import SecretStore


async def create_handler(
    client_class: Type[ClientInterface],
    handler_class: Type[HandlerInterface],
    workflow_args: Dict[str, Any],
) -> HandlerInterface:
    """Create and load a handler from credentials in workflow_args.

    Supports two credential modes:
    - credential_guid: fetches credentials from SecretStore then calls handler.load()
    - credentials: calls handler.load() with the dict directly

    Args:
        client_class: The client class to instantiate.
        handler_class: The handler class to instantiate.
        workflow_args: Must contain either 'credential_guid' or 'credentials'.

    Returns:
        A loaded handler instance.

    Raises:
        ValueError: If neither 'credential_guid' nor 'credentials' is present.
    """
    client = client_class()
    handler = handler_class(client=client)  # type: ignore[call-arg]

    if "credential_guid" in workflow_args:
        credentials = await SecretStore.get_credentials(
            credential_guid=workflow_args["credential_guid"]
        )
        await handler.load(credentials)
    elif "credentials" in workflow_args:
        await handler.load(workflow_args["credentials"])
    else:
        raise ValueError(
            "workflow_args must contain either 'credential_guid' or 'credentials'."
        )

    return handler
