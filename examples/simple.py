"""
Setup:
1. Get your API key from https://cloud.browser-use.com/new-api-key
2. Set environment variable: export BROWSER_USE_API_KEY="your-key"
"""

from dotenv import load_dotenv

from browser_use import Agent, ChatGoogle

load_dotenv()

agent = Agent(
	task='Who are founders of Browser Use',
	llm=ChatGoogle(model='gemini-3-pro-preview'),
)
agent.run_sync()
