# AGENTS.md Version 3

<guidelines>

Browser Use is an AI agent library that interacts with the web through Chromium and the Chrome DevTools Protocol. A task is processed by an explicitly configured language model, which selects browser actions until the task finishes.

## Development rules

- Use `uv` instead of `pip` for environments and dependency management:

```bash
uv venv --python 3.12
source .venv/bin/activate
uv sync
```

- Do not replace user-provided model names. Model providers add names that may be newer than this repository.
- Use Pydantic v2 models for internal action schemas, task inputs and outputs, and tool I/O.
- Run the relevant pre-commit hooks before submitting a pull request.
- Give every action a descriptive name and docstring.
- Prefer `ActionResult` with structured content so agents can reason about results.
- Use the repository documentation and source as the authority for supported behavior.
- Do not create standalone example files while implementing features. Use inline terminal probes when a temporary experiment is useful.
- Configure an LLM explicitly in code, or set `DEFAULT_LLM`.
- Browser execution is local by default. A caller may connect to an independently managed browser with `cdp_url`; the library does not provision or terminate that browser.

## Quickstart

```python
import asyncio

from browser_use import Agent, ChatOpenAI


async def main():
    agent = Agent(
        task="Find the number one post on Show HN",
        llm=ChatOpenAI(model="gpt-4.1-mini"),
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
```

## Browser configuration

```python
from browser_use import Browser

local_browser = Browser(
    headless=False,
    window_size={"width": 1000, "height": 700},
)

managed_elsewhere = Browser(
    cdp_url="http://127.0.0.1:9222",
)
```

`Browser` is an alias for `BrowserSession`.

## Custom tools

Tool parameters injected by the agent must use the exact name `browser_session` and the `BrowserSession` type.

```python
from browser_use import ActionResult, BrowserSession, Tools

tools = Tools()


@tools.action(description="Ask a human for help")
async def ask_human(question: str, browser_session: BrowserSession) -> ActionResult:
    answer = input(f"{question} > ")
    return ActionResult(extracted_content=f"The human responded with: {answer}")
```

</guidelines>
