from abc import ABC, abstractmethod
from typing import Any, Dict, List


class TransformerInterface(ABC):
    @staticmethod
    @abstractmethod
    def transform(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        raise NotImplementedError
