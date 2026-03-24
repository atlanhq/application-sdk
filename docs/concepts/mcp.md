# Model Context Protocol (MCP) Integration

The Application SDK provides built-in support for **Model Context Protocol (MCP)**, enabling your applications to work seamlessly with AI assistants like Claude Desktop, Claude Code, Cursor, and other MCP-compatible clients.

## Overview

MCP integration allows you to expose your application's activities as AI tools that can be discovered and used by AI assistants. This creates a powerful bridge between your Atlan workflows and AI-powered automation.

## Key Features

- **Zero-code AI integration**: Simply add the `@mcp_tool` decorator to existing activities
- **Automatic parameter flattening**: Pydantic models are automatically expanded into individual parameters for better AI experience
- **FastMCP 2.0 compatibility**: Uses the latest MCP server implementation with streamable HTTP transport
- **Hot-pluggable**: Enable/disable MCP without changing your core application logic

## Installation

### Basic Installation

In order to enable MCP support for your existing app, you need to add the `mcp` group to the `atlan-application-sdk` package in your dependencies. You can find an example [here](https://github.com/atlanhq/atlan-sample-apps/blob/main/quickstart/giphy/pyproject.toml#L10).

After that, you need to run the following command to install the relevant dependencies

```bash
uv sync --all-extras --all-groups
```

## How it Works

When you enable MCP support for your application, the SDK will automatically discover all activities marked with the `@mcp_tool` decorator and expose them as tools.

The MCP server is automatically mounted at the root endpoint (`/mcp`) and can be accessed by AI assistants.

## Quick Start

### 1. Mark Activities as Tools

Use the `@mcp_tool` decorator to expose activities as tools:

```python
from application_sdk.decorators.mcp_tool import mcp_tool
from application_sdk.activities import ActivitiesInterface
from temporalio import activity

class MyActivities(ActivitiesInterface):
    @activity.defn
    @mcp_tool(description="Fetch data from external API")
    async def fetch_data(self, query: str) -> dict:
        # Your existing activity code unchanged
        return {"result": f"Data for {query}"}

    @activity.defn
    @mcp_tool(description="Send notification with custom message")
    async def send_notification(self, message: str, priority: str = "normal") -> str:
        # Activity implementation
        return f"Notification sent: {message} (priority: {priority})"
```

### 2. Enable MCP in Your Application

You can enable MCP support by setting the `ENABLE_MCP` environment variable to `true`.

### 3. Start Your Application

When MCP is enabled, your application will automatically:

- Discover all `@mcp_tool` decorated activities
- Create MCP-compatible wrappers
- Mount the MCP server at the root endpoint (`/mcp`)
- Log debug information for AI client configuration

```bash
python main.py
```

You should see logs like:

```
Mounted MCP at root - MCP endpoint: http://localhost:8000/mcp | Transport: streamable_http | Debug with MCP Inspector using the above URL
```

## AI Client Configuration

### Claude Desktop

Add your application to Claude Desktop's MCP configuration:

```json
{
  "mcpServers": {
    "My Atlan App": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8000/mcp"]
    }
  }
}
```

### Other MCP Clients

For any MCP-compatible client, use the streamable HTTP transport:

- **Endpoint**: `http://localhost:8000/mcp`
- **Transport**: `streamable_http`

## Advanced Features

### Custom Tool Names and Descriptions

Customize how your tools appear to AI assistants:

```python
@mcp_tool(
    name="data_fetcher",
    description="Retrieve and process data from external APIs with advanced filtering"
)
async def fetch_data(self, query: str, filters: dict = None) -> dict:
    # Implementation
    pass
```

### Conditional Tool Exposure

Control when tools are available:

```python
@mcp_tool(
    description="Admin-only data export function",
    visible=False  # Disable this tool
)
async def export_sensitive_data(self, format: str) -> dict:
    # This tool won't be exposed to AI assistants
    pass
```

## Development and Debugging

### MCP Inspector

Use the [MCP Inspector](https://modelcontextprotocol.io/legacy/tools/inspector) to test your tools:

1. Install and start the MCP Inspector
2. Enter your endpoint: `http://localhost:8000/mcp`
3. Select transport: `streamable_http`
4. Test your tools interactively

## Configuration

### Environment Variables

Control MCP behavior via environment variables:

```bash
# Enable MCP
ENABLE_MCP=true
```

## Best Practices

### Tool Design

1. **Clear descriptions**: Write descriptive tool descriptions that explain what the tool does and when to use it
2. **Focused functionality**: Each tool should do one thing well
3. **Meaningful parameters**: Use descriptive parameter names and provide default values where appropriate

### Error Handling

```python
@mcp_tool(description="Safe data processing with error handling")
async def process_data(self, data_id: str) -> str:
    try:
        # Process data
        result = await process_data_safely(data_id)
        return f"Processed successfully: {result}"
    except Exception as e:
        # Return user-friendly error messages
        return f"Processing failed: {str(e)}"
```

### Performance Considerations

1. **Lightweight tools**: Keep tool execution fast for better AI experience
2. **Async operations**: Use async/await for I/O operations
3. **Caching**: Implement caching for frequently accessed data

## Troubleshooting

### Common Issues

**MCP server not starting**:

- Ensure `atlan-application-sdk[mcp]` is installed
- Check that `enable_mcp=True` is set
- Verify no port conflicts on 8000

**Tools not appearing in AI client**:

- Confirm `@mcp_tool` decorator is applied correctly
- Check MCP client configuration
- Verify endpoint accessibility

**AI getting parameter errors**:

- Ensure Pydantic models have clear field descriptions
- Provide appropriate default values
- Use simple parameter types when possible
