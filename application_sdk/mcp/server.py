"""
MCP Server implementation for Atlan Application SDK.

This module provides the MCPServer class that automatically discovers
activities marked with @mcp_tool decorators and exposes them via the
Model Context Protocol.
"""

import inspect
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, Callable

from application_sdk.activities import ActivitiesInterface
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server import ServerInterface
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class MCPServer(ServerInterface):
    """
    MCP Server that auto-discovers and exposes decorated activities as MCP tools.
    
    This server scans activities for @mcp_tool decorators and automatically
    creates MCP tools from them, handling the MCP protocol details.
    """
    
    def __init__(self, 
                 mcp_name: str,
                 workflow_client=None,
                 handler=None):
        """
        Initialize the MCP server.
        
        Args:
            mcp_name (str): Name of the MCP server
            workflow_client: Temporal workflow client for activity execution
            handler: Application handler (inherited from ServerInterface)
        """
        super().__init__(handler=handler)
        self.mcp_name = mcp_name
        self.workflow_client = workflow_client
        self.mcp_server = None
        self.registered_tools = []
        
    def discover_and_register_tools(self, 
                                   workflow_and_activities_classes: List[Tuple[Type[WorkflowInterface], Type[ActivitiesInterface]]]):
        """
        Discover activities marked with @mcp_tool and register them as MCP tools.
        
        Args:
            workflow_and_activities_classes: List of (workflow_class, activities_class) tuples
        """
        try:
            from mcp.server.fastmcp import FastMCP, Context
        except ImportError:
            logger.error("MCP dependencies not installed. Run: pip install 'mcp[cli]'")
            return
            
        # Create FastMCP server
        self.mcp_server = FastMCP(self.mcp_name)
        
        for workflow_class, activities_class in workflow_and_activities_classes:
            # Create activities instance 
            activities_instance = activities_class()
            
            # Get all activity methods (reuse existing SDK logic)
            activity_methods = workflow_class.get_activities(activities_instance)
            
            # Register each method marked with @mcp_tool
            for method in activity_methods:
                if self._is_mcp_tool(method):
                    self._register_activity_as_mcp_tool(method, activities_class)
                    
        logger.info(f"Registered {len(self.registered_tools)} MCP tools")
        
        # Add app status resource
        self._add_app_resources()
    
    def _is_mcp_tool(self, method) -> bool:
        """Check if method is marked with @mcp_tool decorator."""
        return hasattr(method, '_is_mcp_tool') and method._is_mcp_tool
    
    def _register_activity_as_mcp_tool(self, activity_method, activities_class: Type[ActivitiesInterface]):
        """Register a single activity as an MCP tool."""
        tool_name = getattr(activity_method, '_mcp_name', activity_method.__name__)
        tool_description = getattr(activity_method, '_mcp_description', activity_method.__doc__ or f"Execute {activity_method.__name__}")
        
        # Get function signature for parameter mapping
        sig = inspect.signature(activity_method)
        
        @self.mcp_server.tool()
        async def mcp_tool_wrapper(*args, ctx, **kwargs):
            """Dynamically created MCP tool wrapper."""
            try:
                await ctx.info(f"🎬 Executing activity: {tool_name}")
                
                # Create fresh activities instance
                activities = activities_class()
                
                # Call the original activity method
                result = await activity_method(*args, **kwargs)
                
                await ctx.info(f"✅ Activity {tool_name} completed successfully")
                return result
                
            except Exception as e:
                await ctx.error(f"❌ Activity {tool_name} failed: {str(e)}")
                raise
        
        # Set metadata for the generated tool
        mcp_tool_wrapper.__name__ = tool_name
        mcp_tool_wrapper.__doc__ = tool_description
        
        self.registered_tools.append({
            "name": tool_name,
            "description": tool_description,
            "original_method": activity_method.__name__
        })
        
        logger.debug(f"Registered MCP tool: {tool_name}")
    
    def _add_app_resources(self):
        """Add standard app resources for MCP."""
        
        @self.mcp_server.resource(f"{self.mcp_name.lower().replace(' ', '_')}://tools")
        def get_available_tools() -> Dict[str, Any]:
            """Get list of available MCP tools."""
            return {
                "app_name": self.mcp_name,
                "tools": self.registered_tools,
                "total_tools": len(self.registered_tools)
            }
        
        @self.mcp_server.resource(f"{self.mcp_name.lower().replace(' ', '_')}://status")
        def get_app_status() -> Dict[str, Any]:
            """Get application status and health."""
            return {
                "status": "healthy",
                "app_name": self.mcp_name,
                "mcp_enabled": True,
                "tools_registered": len(self.registered_tools)
            }
    
    async def start(self, transport: str = "stdio") -> None:
        """
        Start the MCP server.
        
        Args:
            transport (str): Transport protocol ("stdio" or "sse")
        """
        if self.mcp_server is None:
            logger.error("MCP server not initialized. Call discover_and_register_tools() first.")
            return
            
        logger.info(f"🚀 Starting {self.mcp_name} MCP Server with {len(self.registered_tools)} tools")
        
        if transport == "stdio":
            await self.mcp_server.run_stdio_async()
        elif transport == "sse":
            await self.mcp_server.run_sse_async()
        else:
            raise ValueError(f"Unsupported transport: {transport}") 