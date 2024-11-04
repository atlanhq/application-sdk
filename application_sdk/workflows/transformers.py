from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class TransformerInterface(ABC):
    @abstractmethod
    def transform_metadata(
        self, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """
        Transform the metadata into a json string.

        Args:
            typename (str): The type of the metadata.
            data (Dict[str, Any]): The metadata.
            **kwargs (Any): Additional arguments.

        Returns:
            Optional[str]: The json string of the transformed metadata.
        """
        raise NotImplementedError
