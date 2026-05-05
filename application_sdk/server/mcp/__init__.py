from typing import TYPE_CHECKING

from application_sdk.server.mcp.models import MCPMetadata

if TYPE_CHECKING:
    from application_sdk.server.mcp.server import MCPServer

__all__ = ["MCPMetadata", "MCPServer"]


def __getattr__(name: str):
    if name == "MCPServer":
        from application_sdk.server.mcp.server import MCPServer  # noqa: PLC0415

        return MCPServer
    raise AttributeError(
        f"module 'application_sdk.server.mcp' has no attribute {name!r}"
    )
