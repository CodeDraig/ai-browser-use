# Integrations (MCP and Docs)

## Table of Contents
- [MCP Server (Cloud)](#mcp-server-cloud)
- [MCP Server (Local)](#mcp-server-local)
- [Documentation MCP](#documentation-mcp)

---

## MCP Server (Cloud)

HTTP-based MCP server at `https://api.browser-use.com/mcp`

### Setup

**Claude Code:**
```bash
claude mcp add --transport http browser-use https://api.browser-use.com/mcp
```

**Claude Desktop** (macOS `~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "browser-use": {
      "type": "http",
      "url": "https://api.browser-use.com/mcp",
      "headers": { "x-browser-use-api-key": "your-api-key" }
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "browser-use": {
      "type": "http",
      "url": "https://api.browser-use.com/mcp",
      "headers": { "x-browser-use-api-key": "your-api-key" }
    }
  }
}
```

**Windsurf** (`~/.codeium/windsurf/mcp_config.json`):
```json
{
  "mcpServers": {
    "browser-use": {
      "type": "http",
      "url": "https://api.browser-use.com/mcp",
      "headers": { "x-browser-use-api-key": "your-api-key" }
    }
  }
}
```

### Cloud MCP Tools

| Tool | Cost | Description |
|------|------|-------------|
| `browser_task` | $0.01 + per-step | Run browser automation task |
| `execute_skill` | $0.02 | Execute a skill |
| `list_skills` | Free | List available skills |
| `get_cookies` | Free | Get cookies |
| `list_browser_profiles` | Free | List cloud profiles |
| `monitor_task` | Free | Check task progress |

`browser_task` params: `task` (required), `max_steps` (1-10, default 8), `profile_id` (UUID)

---

## MCP Server (Local)

Free, self-hosted stdio-based server:

```bash
uvx --from 'browser-use[cli]' browser-use --mcp
```

### Claude Desktop Config

macOS (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "browser-use": {
      "command": "/Users/your-username/.local/bin/uvx",
      "args": ["--from", "browser-use[cli]", "browser-use", "--mcp"],
      "env": {
        "OPENAI_API_KEY": "your-key"
      }
    }
  }
}
```

Note: Use full path to `uvx` on macOS/Linux (run `which uvx` to find it).

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
        command="uvx",
        args=["--from", "browser-use[cli]", "browser-use", "--mcp"]
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("browser_navigate", arguments={"url": "https://example.com"})
```

---

## Documentation MCP

Read-only docs access (no browser automation):

**Claude Code:**
```bash
claude mcp add --transport http browser-use-docs https://docs.browser-use.com/mcp
```

**Cursor** (`~/.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "browser-use-docs": { "url": "https://docs.browser-use.com/mcp" }
  }
}
```

No API key needed. Provides API reference, config options, best practices, examples.
