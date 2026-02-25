"""Utility functions for SDR activities."""

from typing import Any, Dict, Tuple, Type

from application_sdk.clients import ClientInterface
from application_sdk.handlers import HandlerInterface
from application_sdk.services.secretstore import SecretStore


async def create_handler(
    client_class: Type[ClientInterface],
    handler_class: Type[HandlerInterface],
    workflow_args: Dict[str, Any],
) -> Tuple[ClientInterface, HandlerInterface]:
    """Create and load a handler from credentials in workflow_args.

    Supports two credential modes:
    - credential_guid: fetches credentials from SecretStore then calls handler.load()
    - credentials: calls handler.load() with the dict directly

    Returns the client alongside the handler so callers can close it
    in a ``finally`` block (SQL clients hold connection-pool resources
    that must be explicitly disposed).

    Args:
        client_class: The client class to instantiate.
        handler_class: The handler class to instantiate.
        workflow_args: Must contain either 'credential_guid' or 'credentials'.

    Returns:
        A (client, handler) tuple.  Callers should ``await client.close()``
        when done.

    Raises:
        ValueError: If neither 'credential_guid' nor 'credentials' is present.
    """
    client = client_class()
    # Instantiate the handler without arguments, then assign the client.
    # BaseHandler expects `client` while BaseSQLHandler expects `sql_client`,
    # so we cannot pass a single keyword to the constructor.  Both base
    # classes accept all-optional __init__ params, making no-arg construction
    # safe, and we set the correct attribute afterwards.
    handler = handler_class()  # type: ignore[call-arg]
    if hasattr(handler, "sql_client"):
        handler.sql_client = client  # type: ignore[assignment]
    else:
        handler.client = client  # type: ignore[assignment]

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

    return client, handler
