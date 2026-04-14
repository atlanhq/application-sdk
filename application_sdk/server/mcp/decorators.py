"""
MCP tool decorator for marking activities as MCP tools.

This module provides the @mcp_tool decorator that developers use to mark
activities for automatic exposure via Model Context Protocol.
"""

from typing import Any, Callable, Optional

from application_sdk.constants import MCP_METADATA_KEY
from application_sdk.server.mcp import MCPMetadata


def mcp_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    visible: bool = True,
    *args,
    **kwargs,
):
    """
    Decorator to mark functions as MCP tools.

    Use this decorator to mark any function as an MCP tool. You can additionally use the `visible`
    parameter to control whether the tool is visible at runtime or not.

    Function parameters that are Pydantic models will be automatically converted into correct JSON schema
    for the tool specification. This is handled by the underlying FastMCP server implementation.

    Args:
        name(Optional[str]): The name of the tool. Defaults to the function name.
        description(Optional[str]): The description of the tool. Defaults to the function docstring.
        visible(bool): Whether the MCP tool is visible at runtime or not. Defaults to True.
        *args: Additional arguments to pass to the tool.
        **kwargs: Additional keyword arguments to pass to the tool.

    Examples:
        >>> @mcp_tool(name="add_numbers", description="Add two numbers", visible=True)
        >>> def add_numbers(self, a: int, b: int) -> int:
        >>>     return a + b


        >>> # Use with Temporal activity decorator
        >>> @activity.defn
        >>> @mcp_tool(name="get_weather", description="Get the weather for a given city")
        >>> async def get_weather(self, city: str) -> str:
        >>>     # ... activity implementation unchanged ...
    """

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        mcp_metadata = MCPMetadata(
            name=name if name else f.__name__,
            description=description if description else f.__doc__,
            visible=visible,
            args=args,
            kwargs=kwargs,
        )

        setattr(f, MCP_METADATA_KEY, mcp_metadata)

        return f

    return decorator
