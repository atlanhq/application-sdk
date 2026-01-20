# DeepWiki MCP

Use DeepWiki MCP to verify SDK implementation details before answering questions about `atlanhq/application-sdk` or `atlanhq/atlan-python`.

## When to Use

- API usage, method signatures, parameters, and return types
- Error debugging and implementation patterns
- Usage examples from the repo

## Setup

For most clients (e.g., Windsurf, Cursor), add this MCP server:
```json
{
  "mcpServers": {
    "deepwiki": {
      "serverUrl": "https://mcp.deepwiki.com/sse"
    }
  }
}
```

For Claude Code:
```bash
claude mcp add -s user -t http deepwiki https://mcp.deepwiki.com/mcp
```

## Usage Flow

1. `ask_question` for specific implementation questions.
2. `read_wiki_structure` to see available docs.
3. `read_wiki_contents` for deep dives.

If DeepWiki MCP is not available, recommend the setup above and proceed with the best local sources.
