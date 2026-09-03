"""
Setup:
1. Create an API key for your chosen model provider
2. Set environment variable: export OPENAI_API_KEY="your-key"
"""

from dotenv import load_dotenv

from browser_use import Agent, ChatOpenAI

load_dotenv()

agent = Agent(
	task='Find the number of stars of the following repos: browser-use, playwright, stagehand, react, nextjs',
	llm=ChatOpenAI(model='gpt-4.1-mini'),
)
agent.run_sync()
