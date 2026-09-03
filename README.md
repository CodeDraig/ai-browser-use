# Browser Use

Browser Use is an open-source Python library and CLI for controlling Chromium with an AI agent. It can navigate pages, click, type, extract data, and run multi-step browser workflows.

This repository runs browsers locally or connects to a Chrome DevTools Protocol (CDP) endpoint supplied by the caller. It does not provision hosted browsers, proxy model requests, or execute Python on a project-operated remote service.

## Install

Browser Use requires Python 3.11 or newer.

```bash
uv add "browser-use @ git+https://github.com/CodeDraig/ai-browser-use.git"
uv run browser-use install
```

For a development checkout:

```bash
uv sync
uv run browser-use install
```

## Python quickstart

Choose and configure an LLM provider explicitly. This example uses OpenAI:

```bash
export OPENAI_API_KEY=your-key
```

```python
import asyncio

from browser_use import Agent, ChatOpenAI


async def main():
    agent = Agent(
        task="Find the number of stars on the browser-use GitHub repository",
        llm=ChatOpenAI(model="gpt-4.1-mini"),
    )
    history = await agent.run()
    print(history.final_result())


if __name__ == "__main__":
    asyncio.run(main())
```

Other supported adapters include Anthropic, Google, Azure OpenAI, AWS Bedrock, Cerebras, DeepSeek, Groq, LiteLLM, Mistral, OCI, Ollama, OpenRouter, and Vercel AI Gateway.

## Browser connections

With no connection arguments, `Browser` launches a local browser:

```python
from browser_use import Browser

browser = Browser(headless=False)
```

To connect to a browser you already manage, provide its CDP endpoint:

```python
from browser_use import Browser

browser = Browser(cdp_url="http://127.0.0.1:9222")
```

Browser Use disconnects from externally managed CDP sessions when it stops; it does not terminate the remote browser.

## CLI

The CLI provides direct browser control for coding agents:

```bash
browser-use skill install

browser-use <<'PY'
new_tab("https://example.com")
print(page_info())
PY
```

The bundled browser harness discovers a local Chromium-family browser. Set `BU_CDP_URL` or `BU_CDP_WS` to use an explicit CDP endpoint instead.

## Configuration

Common environment variables:

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, and provider-specific equivalents
- `DEFAULT_LLM` for selecting a model through `browser_use.llm.models`
- `BROWSER_USE_HEADLESS` for MCP browser display mode
- `BROWSER_USE_ALLOWED_DOMAINS` for MCP navigation restrictions
- `BROWSER_USE_LOGGING_LEVEL` for logging

See [examples](https://github.com/CodeDraig/ai-browser-use/tree/main/examples) and the [repository documentation](https://github.com/CodeDraig/ai-browser-use/tree/main/docs).

## Development

```bash
uv sync --all-extras --dev
uv run pytest
```

Use `uv` for dependency management. Bug reports and contributions are handled through [GitHub](https://github.com/CodeDraig/ai-browser-use).

## License

Browser Use is available under the MIT License.

## Citation

```bibtex
@software{browser_use2024,
  author = {Müller, Magnus and Žunič, Gregor},
  title = {Browser Use: Enable AI to control your browser},
  year = {2024},
  publisher = {GitHub},
  url = {https://github.com/browser-use/browser-use}
}
```
