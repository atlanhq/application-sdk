import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from application_sdk.transformers.errors import TransformerNotImplementedError

if TYPE_CHECKING:
    import pyarrow as pa

warnings.warn(
    "application_sdk.transformers is deprecated and will be removed in the next major version. "
    "Use the connector-side typed-record → mapper-function pattern instead. "
    "See docs/upgrade-guide-v3.md.",
    DeprecationWarning,
    stacklevel=2,
)


class TransformerInterface(ABC):
    """Abstract base class for metadata transformers.

    This class defines the interface for transforming metadata between different
    formats or representations. Implementations should handle the conversion of
    metadata while preserving semantic meaning and relationships.

    All transformer implementations must inherit from this class and implement
    the transform_metadata method.
    """

    @abstractmethod
    def transform_metadata(
        self,
        typename: str,
        dataframe: "pa.Table | list[dict[str, Any]]",
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: dict[str, type[Any]] | None = None,
        **kwargs: Any,
    ) -> "pa.Table | list[dict[str, Any]] | None":
        """Transform metadata from one format to another.

        This method should convert the input metadata into a different format
        while preserving its semantic meaning. The transformation should handle
        type-specific conversions and maintain relationships between entities.

        Args:
            typename (str): The type identifier for the metadata being transformed.
            data (Dict[str, Any]): The source metadata to transform.
            workflow_id (str): Identifier for the workflow requesting the transformation.
            workflow_run_id (str): Identifier for the specific workflow run.
            entity_class_definitions (Dict[str, Type[Any]] | None, optional): Mapping of
                entity types to their class definitions. Defaults to None.
            **kwargs (Any): Additional keyword arguments for specific transformer
                implementations.

        Returns:
            Optional[Dict[str, Any]]: The transformed metadata, or None if the
                transformation is not applicable or possible.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """

        raise TransformerNotImplementedError()
