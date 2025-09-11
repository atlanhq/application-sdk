# Atlan Application SDK

[![On-Push Checks](https://github.com/atlanhq/application-sdk/actions/workflows/push.yaml/badge.svg)](https://github.com/atlanhq/application-sdk/actions/workflows/push.yaml) [![CodeQL Advanced](https://github.com/atlanhq/application-sdk/actions/workflows/codeql.yaml/badge.svg)](https://github.com/atlanhq/application-sdk/actions/workflows/codeql.yaml) [![PyPI version](https://img.shields.io/pypi/v/atlan-application-sdk.svg)](https://pypi.org/project/atlan-application-sdk/)

The Atlan Application SDK is a Python library designed for building applications on the Atlan platform. It offers a full PaaS (Platform-as-a-Service) toolkit — from local development to deployment and partner collaboration — so you can create integrations and tools that seamlessly extend the Atlan experience for our mutual customers.

## Usage

Install `atlan-application-sdk` as a dependency in your project:

- Using pip:

```bash
# pip install the latest version from PyPI
pip install atlan-application-sdk

# For MCP support, install with MCP extras
pip install "atlan-application-sdk[mcp]"
```

- Using alternative package managers:

```bash
# Using uv to install the latest version from PyPI
uv add atlan-application-sdk

# OR using Poetry to install the latest version from PyPI
poetry add atlan-application-sdk

# For MCP support with uv/poetry
uv add "atlan-application-sdk[mcp]"
poetry add "atlan-application-sdk[mcp]"
```

> [!TIP] > **View sample apps built using Application SDK [here](https://github.com/atlanhq/atlan-sample-apps)**

## MCP Integration (Model Context Protocol)

The Application SDK now supports **Model Context Protocol (MCP)**, enabling your applications to work seamlessly with AI assistants like Claude Desktop, Claude Code, Cursor etc.

### Quick Start with AI Integration

1. **Install with MCP support**:

   ```bash
   pip install "atlan-application-sdk[mcp]"
   ```

2. **Mark activities for AI exposure**:

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
   ```

3. **Enable MCP in your application**:

   ```python
   from application_sdk.application import BaseApplication

   # Enable via constructor
   app = BaseApplication(name="my-app", enable_mcp=True)

   # Or via environment variable
   # ENABLE_MCP=true python main.py
   ```

4. **Use with Claude Desktop**:
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

### MCP Dependencies

When you install `atlan-application-sdk[mcp]`, you get:

- `fastmcp>=2.0.0` - Modern MCP server implementation
- Full compatibility with Model Context Protocol specification
- Streamable HTTP transport for AI agent communication

## Getting Started

- Want to develop locally or run examples from this repository? Check out our [Getting Started Guide](docs/docs/guides/getting-started.md) for a step-by-step walkthrough!
- Detailed documentation for the application-sdk is available at [docs](https://github.com/atlanhq/application-sdk/blob/main/docs/docs/) folder.

## Contributing

- We welcome contributions! Please see our [Contributing Guide](https://github.com/atlanhq/application-sdk/blob/main/CONTRIBUTING.md) for guidelines.

## Partner Collaboration

- For information on how to collaborate with Atlan on app development and integrations, please see our [Partner Collaboration Guide](https://github.com/atlanhq/application-sdk/blob/main/docs/docs/guides/partners.md).

## Need help?

We’re here whenever you need us:

- Email: **connect@atlan.com**
- Issues: [GitHub Issues](https://github.com/atlanhq/application-sdk/issues)

## Security

Have you discovered a vulnerability or have concerns about the SDK? Please read our [SECURITY.md](https://github.com/atlanhq/application-sdk/blob/main/SECURITY.md) document for guidance on responsible disclosure, or please e-mail security@atlan.com and we will respond promptly.

## License and Attribution

- This project is licensed under the Apache License 2.0 - see the [LICENSE](https://github.com/atlanhq/application-sdk/blob/main/LICENSE) file for details.
- This project includes dependencies with various open-source licenses. See the [NOTICE](https://github.com/atlanhq/application-sdk/blob/main/NOTICE) file for third-party attributions.
