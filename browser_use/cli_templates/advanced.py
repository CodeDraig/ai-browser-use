"""Browser Use agent with validated structured output."""

import asyncio

from dotenv import load_dotenv
from pydantic import BaseModel

from browser_use import Agent, ChatOpenAI

load_dotenv()


class SearchResult(BaseModel):
	title: str
	url: str


class SearchResults(BaseModel):
	results: list[SearchResult]


async def main() -> None:
	agent = Agent(
		task='Find the first three posts on Hacker News.',
		llm=ChatOpenAI(model='gpt-4.1-mini'),
		output_model_schema=SearchResults,
	)
	history = await agent.run()
	if history.structured_output is not None:
		print(history.structured_output.model_dump_json(indent=2))


if __name__ == '__main__':
	asyncio.run(main())
