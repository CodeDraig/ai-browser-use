"""Minimal Browser Use agent using a local browser and an explicit LLM provider."""

import asyncio

from dotenv import load_dotenv

from browser_use import Agent, ChatOpenAI

load_dotenv()


async def main() -> None:
	agent = Agent(
		task='Find the top post on Hacker News and summarize it.',
		llm=ChatOpenAI(model='gpt-4.1-mini'),
	)
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
