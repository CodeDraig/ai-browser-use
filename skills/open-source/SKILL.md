---
name: open-source
description: >
  Documentation reference for writing Python code using the browser-use
  open-source library. Use this skill whenever the user needs help with
  Agent, Browser, or Tools configuration, is writing code that imports
  from browser_use, supported LLM models,
  Actor API, custom tools, lifecycle hooks, MCP server setup, or
  external instrumentation or cost tracking. Also trigger for
  questions about browser-use installation, prompting strategies, or
  sensitive data handling. Do NOT use this for directly
  automating a browser via CLI commands — use the browser-use skill instead.
allowed-tools: Read
---

# Browser Use Open-Source Library Reference

Reference docs for writing Python code against the browser-use library.
Read the relevant file based on what the user needs.

| Topic | Read |
|-------|------|
| Install, quickstart, and local development | `references/quickstart.md` |
| LLM providers (15+): setup, env vars, pricing | `references/models.md` |
| Agent params, output, prompting, hooks, timeouts | `references/agent.md` |
| Browser params, auth, real browser, explicit CDP | `references/browser.md` |
| Custom tools, built-in tools, ActionResult | `references/tools.md` |
| Actor API: Page/Element/Mouse | `references/actor.md` |
| Local MCP server and skills | `references/integrations.md` |
| External instrumentation, OpenLIT, cost tracking | `references/monitoring.md` |
| Fast agent, parallel, playwright, sensitive data | `references/examples.md` |

## Critical Notes

- Configure a supported LLM provider explicitly, including its model name
- The library is async Python >= 3.11. Entry points use `asyncio.run()`
- `Browser` is the preferred package-root name for the `BrowserSession` implementation
- Use `uv` for dependency management, never `pip`
- Install this fork from its VCS URL, then run `uv run browser-use install`
- Set the API key required by the selected provider
