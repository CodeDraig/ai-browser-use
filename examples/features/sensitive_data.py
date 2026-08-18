import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()

from browser_use import Agent, ChatOpenAI

# Initialize the model
llm = ChatOpenAI(
	model='gpt-4.1',
	temperature=0.0,
)
# Define a single credential set that can be reused
company_credentials: dict[str, str] = {'telephone': '9123456789', 'email': 'user@example.com', 'name': 'John Doe'}

# Map the same credentials to multiple domains for secure access control
sensitive_data: dict[str, dict[str, str]] = {
	# 'https://example.com': company_credentials,
	# 'https://admin.example.com': company_credentials,
	# 'https://*.example-staging.com': company_credentials,
	# 'http*://test.example.com': company_credentials,
	'httpbin.org': company_credentials,
	# # You can also add domain-specific credentials
	# 'https://google.com': {'g_email': 'user@gmail.com', 'g_pass': 'google_password'}
}
# Update task to use one of the credentials above
task = 'Go to https://httpbin.org/forms/post and put the secure information in the relevant fields.'

agent = Agent(task=task, llm=llm, sensitive_data=sensitive_data)


async def main():
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
