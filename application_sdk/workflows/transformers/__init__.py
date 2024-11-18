from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional


class TransformerInterface(ABC):
    @abstractmethod
    def transform_metadata(
        self,
        typename: str,
        data: Dict[str, Any],
        entity_creator_map: Dict[str, Callable[[Dict[str, Any]], Optional[Any]]]
        | None = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Transform the metadata into a json string.

        Args:
            entity_creator_map (Dict[str, Callable[[Dict[str, Any]], Optional[Any]]] | None): The entity creator map.
            typename (str): The type of the metadata.
            data (Dict[str, Any]): The metadata.
            **kwargs (Any): Additional arguments.

        Returns:
            Optional[str]: The json string of the transformed metadata.
        """
        raise NotImplementedError
