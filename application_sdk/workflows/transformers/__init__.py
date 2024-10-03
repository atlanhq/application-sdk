from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class TransformerInterface(ABC):
    @staticmethod
    @abstractmethod
    def transform_metadata(
        connector_name: str, connector_type: str, typename: str, data: Dict[str, Any]
    ) -> Optional[Any]:
        raise NotImplementedError
