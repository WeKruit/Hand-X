"""
Browser Use Cloud Demo - Simple Web Scraping
Demonstrates Browser Use's cloud-hosted browser for web scraping.
"""

import asyncio

from dotenv import load_dotenv

from browser_use import Agent, Browser, remote_execute
from browser_use.llm.browser_use.chat import ChatBrowserUse
from browser_use.remote_execute.views import BrowserCreatedData

# Load environment variables
load_dotenv()


async def on_browser_created(browser: BrowserCreatedData):
	print(f'Browser created: {browser.live_url}')


@remote_execute(log_level='INFO', on_browser_created=on_browser_created)
async def runner(browser: Browser):
	print('\nBrowser Use Cloud Demo - Hacker News Top Articles')
	print('=' * 50)
	print('Extracting top 3 Hacker News articles with summaries...\n')

	# configure any agent provider
	llm = ChatBrowserUse()
	# llm=ChatAnthropic(model='claude-sonnet-4-0')
	# llm = ChatOpenAI(model="chatgpt-4o-latest")
	# llm = ChatGroq(model= "moonshotai/kimi-k2-instruct")o

	agent = Agent(
		task=(
			"""
           Go to news.ycombinator.com and for each of the top 3 articles, list: 
           title: 
           url:  
           summary: 
           separated by newlines. 
           """
		),
		llm=llm,
		browser=browser,
	)

	result = await agent.run()
	print(result.structured_output)

	return result


async def main():
	history = await runner()
	print(history)


if __name__ == '__main__':
	asyncio.run(main())
