from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from pydantic.v1 import BaseModel


class TransformerInterface(ABC):
    @abstractmethod
    def transform_metadata(
        self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[BaseModel]:
        """
        Transform the metadata into a BaseModel.

        Args:
            connector_name (str): The name of the connector.
            typename (str): The type of the metadata.
            data (Dict[str, Any]): The metadata.
            **kwargs (Any): Additional arguments.

        Returns:
            Optional[BaseModel]: The transformed metadata.
        """
        raise NotImplementedError
