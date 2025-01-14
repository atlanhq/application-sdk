from abc import ABC
from typing import Any, Dict


class ClientInterface(ABC):
    def __init__(self):
        pass

    async def load(self):
        pass

    async def close(self):
        pass

    def set_credentials(self, credentials: Dict[str, Any]):
        pass
