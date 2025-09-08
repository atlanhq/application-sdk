"""
MCP decorators for marking activities as MCP tools.

This module provides decorators that application developers can use to mark
activities for automatic exposure as MCP tools.
"""

from typing import Optional


def mcp_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    enabled: bool = True
):
    """
    Decorator to mark an activity for MCP tool exposure.
    
    This decorator marks a Temporal activity to be automatically exposed
    as an MCP tool when the application runs in MCP mode.
    
    Args:
        name (Optional[str]): Custom name for the MCP tool. 
                            Defaults to the function name.
        description (Optional[str]): Description for the MCP tool.
                                   Defaults to the function docstring.
        enabled (bool): Whether the tool should be enabled. 
                       Defaults to True.
    
    Example:
        @activity.defn
        @mcp_tool(description="Fetch random GIF from Giphy API")
        async def fetch_gif(self, search_term: str) -> str:
            # ... activity implementation ...
    """
    def decorator(func):
        # Mark function as MCP tool
        func._is_mcp_tool = enabled
        func._mcp_name = name or func.__name__
        func._mcp_description = description or func.__doc__ or f"Execute {func.__name__}"
        return func
    return decorator 