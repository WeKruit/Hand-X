"""
Setup:
1. Get your API key from https://cloud.browser-use.com/new-api-key
2. Set environment variable: export BROWSER_USE_API_KEY="your-key"
"""

import asyncio
import os
import sys

# Add the parent directory to the path so we can import browser_use
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()

from browser_use import Agent, ChatBrowserUse
from browser_use.llm import ChatGoogle


async def main():
	llm = ChatBrowserUse()
	# Use Claude for judging since it supports vision + structured output
	judge_llm = ChatGoogle(model='gemini-flash-latest')
	task = "Search Google for 'what is browser automation' and tell me the top 3 results"
	agent = Agent(task=task, llm=llm, judge_llm=judge_llm)
	history = await agent.run()

	# Print the judgement result
	if history.is_judged():
		judgement = history.judgement()
		print('\n' + '=' * 80)
		print('JUDGE EVALUATION')
		print(judgement)
	else:
		print('\nNo judgement available (task may not have completed or use_judge=False)')


if __name__ == '__main__':
	asyncio.run(main())
