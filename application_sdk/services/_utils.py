import asyncio

from application_sdk.infrastructure._dapr.http import AsyncDaprClient
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def is_component_registered(component_name: str) -> bool:
    """Check if a DAPR component with the given name is registered.

    Args:
        component_name: Name of the component to check.

    Returns:
        True if the component is present, False otherwise or on metadata errors.
    """
    try:

        async def _check():
            client = AsyncDaprClient()
            try:
                metadata = await client.get_metadata()
                # Dapr 1.13: "registeredComponents", 1.14+: "components"
                components = metadata.get(
                    "components", metadata.get("registeredComponents", [])
                )
                return any(c.get("name") == component_name for c in components)
            finally:
                await client.close()

        return asyncio.run(_check())
    except Exception:
        logger.warning(
            "Failed to read Dapr metadata for component %s; treating as unavailable",
            component_name,
            exc_info=True,
        )
        return False
