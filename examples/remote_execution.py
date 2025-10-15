"""Example of using remote execution with Browser-Use Agent

This example demonstrates how to use the @remote_execute decorator to run
browser automation tasks with the Agent on remote infrastructure.

To run this example:
1. Set your BROWSER_USE_API_KEY environment variable
2. Set your LLM API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
3. Run: python examples/remote_execution.py
"""

import asyncio
import os

from browser_use import AgentHistoryList, Browser, ChatBrowserUse, remote_execute
from browser_use.agent.service import Agent


# Example with event callbacks to monitor execution
def on_browser_ready(data):
	"""Callback when browser session is created"""
	print('\n🌐 Browser session created!')
	print(f'   Session ID: {data.session_id}')
	print(f'   Live view: {data.live_url}')
	print('   Click the link above to watch the AI agent work!\n')


@remote_execute(log_level='INFO', on_browser_created=on_browser_ready)
async def pydantic_example(browser: Browser) -> AgentHistoryList:
	agent = Agent(
		"""got of to https://news.ycombinator.com/ and find the latest top 5 news and each top comment""",
		browser=browser,
		# output_model_schema=HackernewsPosts,
		llm=ChatBrowserUse(),
	)
	res = await agent.run()

	return res


async def main():
	"""Run examples"""
	# Check if API keys are set
	if not os.getenv('BROWSER_USE_API_KEY'):
		print('❌ Please set BROWSER_USE_API_KEY environment variable')
		return

	print('\n\n=== Search with AI Agent (with live browser view) ===')

	search_result = await pydantic_example()

	print(f'\n✅ Search completed with {len(search_result.history)} actions')
	print('\nResults:')
	print(search_result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
