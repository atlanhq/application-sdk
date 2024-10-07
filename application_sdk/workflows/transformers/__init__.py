from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from pydantic.v1 import BaseModel


class TransformerInterface(ABC):
    @abstractmethod
    def transform_metadata(
        self, connector_name: str, typename: str, data: Dict[str, Any], **kwargs: Any
    ) -> Optional[BaseModel]:
        raise NotImplementedError
