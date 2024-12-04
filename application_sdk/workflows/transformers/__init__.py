from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type


class TransformerInterface(ABC):
    @abstractmethod
    def transform_metadata(
        self,
        typename: str,
        data: Dict[str, Any],
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Transform the metadata into a json string.

        Args:
            entity_class_definitions (Dict[str, Type[Any]] | None): The entity class definitions.
            typename (str): The type of the metadata.
            data (Dict[str, Any]): The metadata.
            **kwargs (Any): Additional arguments.

        Returns:
            Optional[str]: The json string of the transformed metadata.
        """
        raise NotImplementedError
