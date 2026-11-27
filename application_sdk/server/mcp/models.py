from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel

__all__ = [
    "MCPMetadata",
]


class MCPMetadata(BaseModel):
    name: str
    description: Optional[str]
    visible: bool
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = {}
