from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel


class MCPMetadata(BaseModel):
    name: str
    description: Optional[str]
    visible: bool
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = {}
