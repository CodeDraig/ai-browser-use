# Local MCP Integration

## Table of Contents
- [MCP Server](#mcp-server)

---

## MCP Server

Free, self-hosted stdio-based server:

```bash
browser-use --mcp
```

### Claude Desktop Config

macOS (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "browser-use": {
      "command": "/absolute/path/to/browser-use",
      "args": ["--mcp"],
      "env": {
        "OPENAI_API_KEY": "your-key"
      }
    }
  }
}
```

Note: If the MCP client does not inherit your shell PATH, use the full path reported by `which browser-use`.

### Local MCP Tools

**Agent:** `retry_with_browser_use_agent` — full automation task

**Direct Control:**
- `browser_navigate` — Go to URL
- `browser_click` — Click element by index
- `browser_type` — Type text
- `browser_get_state` — Page state + interactive elements
- `browser_scroll` — Scroll page
- `browser_go_back` — Back in history

**Tabs:** `browser_list_tabs`, `browser_switch_tab`, `browser_close_tab`

**Extraction:** `browser_extract_content` — Structured extraction

**Sessions:** `browser_list_sessions`, `browser_close_session`, `browser_close_all`

### Environment Variables

- `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` — LLM key (required)
- `BROWSER_USE_HEADLESS` — `false` to show browser
- `BROWSER_USE_DISABLE_SECURITY` — `true` to disable security
- `BROWSER_USE_LOGGING_LEVEL` — `DEBUG` for verbose logs

### Programmatic Usage

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def use_browser_mcp():
    server_params = StdioServerParameters(
        command="browser-use",
        args=["--mcp"]
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("browser_navigate", arguments={"url": "https://example.com"})
```

---
