from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Type

if TYPE_CHECKING:
    import daft


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
        dataframe: "daft.DataFrame",
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
        **kwargs: Any,
    ) -> "daft.DataFrame":
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
        raise NotImplementedError
