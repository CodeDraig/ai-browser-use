"""Browser Use agent with a typed custom action."""

import asyncio

from dotenv import load_dotenv
from pydantic import BaseModel

from browser_use import ActionResult, Agent, ChatOpenAI, Tools

load_dotenv()

tools = Tools()


class TextToCount(BaseModel):
	text: str


@tools.action(description='Count the characters in text', param_model=TextToCount)
async def count_characters(params: TextToCount) -> ActionResult:
	return ActionResult(extracted_content=f'The text contains {len(params.text)} characters.')


async def main() -> None:
	agent = Agent(
		task='Find the title of the top Hacker News post, then count its characters.',
		llm=ChatOpenAI(model='gpt-4.1-mini'),
		tools=tools,
	)
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
