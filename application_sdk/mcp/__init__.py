"""
Model Context Protocol (MCP) integration for Atlan Application SDK.

This module provides decorators and utilities to automatically expose
activities as MCP tools, enabling AI agents to interact with Atlan applications.
"""

from application_sdk.decorators.mcp_tool import mcp_tool

__all__ = ["mcp_tool"]
