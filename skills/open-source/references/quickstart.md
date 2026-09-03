# Quickstart & Local Development

## Table of Contents
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [First Agent](#first-agent)
- [Local Development](#local-development)

---

## Installation

```bash
pip install uv
uv venv --python 3.12
source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install "browser-use @ git+https://github.com/CodeDraig/ai-browser-use.git"
browser-use install     # Downloads Chromium
```

## Environment Variables

```bash
# Google — https://aistudio.google.com/app/u/1/apikey
GOOGLE_API_KEY=

# OpenAI
OPENAI_API_KEY=

# Anthropic
ANTHROPIC_API_KEY=
```

## First Agent

### Google Gemini

```python
from browser_use import Agent, ChatGoogle
from dotenv import load_dotenv
import asyncio

load_dotenv()

async def main():
    llm = ChatGoogle(model="gemini-3-flash-preview")
    agent = Agent(task="Find the number 1 post on Show HN", llm=llm)
    await agent.run()

if __name__ == "__main__":
    asyncio.run(main())
```

### OpenAI

```python
from browser_use import Agent, ChatOpenAI
from dotenv import load_dotenv
import asyncio

load_dotenv()

async def main():
    llm = ChatOpenAI(model="gpt-4.1-mini")
    agent = Agent(task="Find the number 1 post on Show HN", llm=llm)
    await agent.run()

if __name__ == "__main__":
    asyncio.run(main())
```

### Anthropic

```python
from browser_use import Agent, ChatAnthropic
from dotenv import load_dotenv
import asyncio

load_dotenv()

async def main():
    llm = ChatAnthropic(model='claude-sonnet-4-0', temperature=0.0)
    agent = Agent(task="Find the number 1 post on Show HN", llm=llm)
    await agent.run()

if __name__ == "__main__":
    asyncio.run(main())
```

See `references/open-source/models.md` for all 15+ providers.

---

## Local Development

```bash
git clone https://github.com/CodeDraig/ai-browser-use
cd ai-browser-use
uv sync --all-extras --dev

# Helper scripts
./bin/setup.sh   # Complete setup
./bin/lint.sh    # Formatting, linting, type checking
./bin/test.sh    # CI test suite

# Run examples
uv run examples/simple.py
```
