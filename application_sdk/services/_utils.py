from dapr import clients

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
        with clients.DaprClient() as client:
            metadata = client.get_metadata()
            # Each registered component has fields: name, type (e.g., "eventstore")
            for component in getattr(metadata, "registered_components", []):
                if component.name == component_name:
                    return True
            return False
    except Exception:
        # If we cannot read metadata, behave conservatively and report unavailable
        logger.warning(
            "Failed to read Dapr metadata for component availability check; treating as unavailable",
            exc_info=True,
            extra={"component_name": component_name},
        )
        return False
