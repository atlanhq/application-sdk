"""
MCP tool decorator for marking activities as MCP tools.

This module provides the @mcp_tool decorator that developers use to mark
activities for automatic exposure via Model Context Protocol.
"""

from typing import Optional


def mcp_tool(
    description: Optional[str] = None, name: Optional[str] = None, enabled: bool = True
):
    """
    Decorator to mark activities for MCP tool exposure.

    When an activity is decorated with @mcp_tool, it will be automatically
    discovered and exposed as an MCP tool when the application runs with
    enable_mcp=True.

    Args:
        description (Optional[str]): Description for the MCP tool.
                                   Defaults to the function docstring.
        name (Optional[str]): Custom name for the MCP tool.
                            Defaults to the function name.
        enabled (bool): Whether this tool should be enabled.
                       Defaults to True.

    Example:
        @activity.defn
        @mcp_tool(description="Fetch random GIF from Giphy API")
        async def fetch_gif(self, search_term: str) -> str:
            # ... activity implementation unchanged ...
    """

    def decorator(func):
        # Mark function with MCP metadata
        func._is_mcp_tool = enabled
        func._mcp_name = name or func.__name__
        func._mcp_description = (
            description or func.__doc__ or f"Execute {func.__name__}"
        )
        return func

    return decorator
