"""
Setup:
1. Get your API key from https://cloud.browser-use.com/dashboard/api
2. Set environment variable: export BROWSER_USE_API_KEY="your-key"
"""

from dotenv import load_dotenv

from browser_use import Agent, ChatBrowserUse

load_dotenv()

agent = Agent(
	task='https://wcpdev.wd101.myworkday.com/wday/authgwy/nayyabow_wcpdev2/login.html?returnTo=%2fnayyabow_wcpdev2%2fd%2fhome.html go here click and then fill with example data, use only send keys ',
	# task='go to duckduckgo.com search for browser-use but use only send keys validate the results, go to first link',
	llm=ChatBrowserUse(),
)
agent.run_sync()
