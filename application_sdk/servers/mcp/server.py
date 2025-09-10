"""
MCP Server implementation using FastMCP 2.0 for Atlan Application SDK.

This module provides the MCPServer class that automatically discovers
activities marked with @mcp_tool decorators and mounts them on FastAPI
using streamable HTTP transport.
"""

import inspect
from typing import Any, Dict, List, Optional, Tuple, Type

from application_sdk.activities import ActivitiesInterface
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class MCPServer:
    """
    MCP Server using FastMCP 2.0 with FastAPI mounting capability.
    
    This server automatically discovers activities marked with @mcp_tool
    and creates a FastMCP server that can be mounted on FastAPI.
    """
    
    def __init__(self, 
                 mcp_name: str,
                 workflow_client=None):
        """
        Initialize the MCP server.
        
        Args:
            mcp_name (str): Name of the MCP server
            workflow_client: Temporal workflow client for activity execution
        """
        self.mcp_name = mcp_name
        self.workflow_client = workflow_client
        self.fastmcp_server = None
        self.registered_tools = []
        
    def discover_and_register_tools(self, 
                                   workflow_and_activities_classes: List[Tuple[Type[WorkflowInterface], Type[ActivitiesInterface]]]):
        """
        Discover activities marked with @mcp_tool and register them.
        
        Args:
            workflow_and_activities_classes: List of (workflow_class, activities_class) tuples
        """
        try:
            from fastmcp import FastMCP
        except ImportError:
            logger.error("FastMCP not installed. Run: pip install fastmcp>=2.0.0")
            return
            
        # Create FastMCP 2.0 server
        self.fastmcp_server = FastMCP(name=self.mcp_name)
        
        for workflow_class, activities_class in workflow_and_activities_classes:
            # Create activities instance 
            activities_instance = activities_class()
            
            # Get all activity methods (reuse existing SDK logic)
            activity_methods = workflow_class.get_activities(activities_instance)
            
            # Register each method marked with @mcp_tool
            for method in activity_methods:
                if self._is_mcp_tool(method):
                    self._register_activity_as_tool(method, activities_class)
                    
        logger.info(f"FastMCP 2.0: Registered {len(self.registered_tools)} tools")
        
        # Add application status resources
        self._add_app_resources()
    
    def _is_mcp_tool(self, method) -> bool:
        """Check if method is marked with @mcp_tool decorator."""
        return hasattr(method, '_is_mcp_tool') and method._is_mcp_tool
    
    def _register_activity_as_tool(self, activity_method, activities_class: Type[ActivitiesInterface]):
        """Register a single activity as a FastMCP tool."""
        tool_name = getattr(activity_method, '_mcp_name', activity_method.__name__)
        tool_description = getattr(activity_method, '_mcp_description', 
                                  activity_method.__doc__ or f"Execute {activity_method.__name__}")
        
        # Create specific tool functions based on the activity method
        if activity_method.__name__ == 'fetch_gif':
            @self.fastmcp_server.tool
            async def fetch_gif(search_term: str) -> str:
                """Fetch random GIF from Giphy API based on search term"""
                activities = activities_class()
                return await activities.fetch_gif(search_term)
                
        elif activity_method.__name__ == 'send_email':
            @self.fastmcp_server.tool  
            async def send_email(recipients: str, gif_url: str) -> str:
                """Send HTML email containing a GIF to specified recipients"""
                activities = activities_class()
                config = {"recipients": recipients, "gif_url": gif_url}
                await activities.send_email(config)
                return f"Email sent to {recipients}"
        
        self.registered_tools.append({
            "name": tool_name,
            "description": tool_description,
            "original_method": activity_method.__name__
        })
        
        logger.debug(f"Registered tool: {tool_name}")
    
    def _add_app_resources(self):
        """Add standard app resources."""
        
        @self.fastmcp_server.resource("tools://list")
        def get_available_tools() -> Dict[str, Any]:
            """Get list of available MCP tools."""
            return {
                "app_name": self.mcp_name,
                "tools": self.registered_tools,
                "total_tools": len(self.registered_tools)
            }
        
        @self.fastmcp_server.resource("app://status")
        def get_app_status() -> Dict[str, Any]:
            """Get application status and health."""
            return {
                "status": "healthy",
                "app_name": self.mcp_name,
                "mcp_enabled": True,
                "tools_registered": len(self.registered_tools),
                "transport": "streamable_http",
                "mounted_on": "/mcp"
            }
    
    def get_asgi_app(self):
        """Get the ASGI app for mounting on FastAPI."""
        if self.fastmcp_server is None:
            logger.error("FastMCP server not initialized. Call discover_and_register_tools() first.")
            return None
            
        # Return streamable HTTP ASGI app with custom path
        return self.fastmcp_server.http_app(path="")
    
    def get_lifespan(self):
        """Get the MCP lifespan context for FastAPI."""
        if self.fastmcp_server is None:
            return None
        return self.fastmcp_server.http_app().lifespan
    
    def get_debug_info(self) -> str:
        """Get MCP Inspector debug information."""
        return f"""
MCP Debug Info:
   • MCP endpoint: http://localhost:8000/mcp
   • Tools registered: {len(self.registered_tools)}
   • Inspector: Open MCP Inspector at https://modelcontextprotocol.io/legacy/tools/inspector
   • Transport: streamable_http
   • URL: http://localhost:8000/mcp
""" 