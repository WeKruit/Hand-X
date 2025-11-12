import asyncio
import os
import sys

from browser_use.llm.google.chat import ChatGoogle

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from browser_use import Agent

try:
	from lmnr import Laminar

	Laminar.initialize(project_api_key=os.getenv('LMNR_PROJECT_API_KEY'))
except Exception as e:
	print(f'Error initializing Laminar: {e}')


# llm = ChatOpenAI(base_url='https://browseruse--browseruse-v0-serve.modal.run/v1',
# 				model='browser-use/Qwen3-VL-8B-Instruct-301025-1epoch',
# 				api_key='aitorloveskebabs',
# 				temperature=1.0,
# 				dont_force_structured_output=True)

llm = ChatGoogle(model='gemini-flash-latest', temperature=1.0)
# llm = ChatAnthropic(model='claude-sonnet-4-5-20250929', temperature=1.0)
# llm = ChatOpenAI(model='gpt-5-mini', temperature=1.0)

task = """IMPORTANT RULE: Use only clicking by coordinate and sending keys actions. Do not use any other actions. Go to https://levelshealth.typeform.com/waitlist-uk and input John into the first name field and click on OK button using coordinates."""
# task = 'go to example.com and use the extract tool to extract the content of the page.'

task = 'How many studio albums were published by Mercedes Sosa between 2000 and 2009 (included)? You can use the latest 2022 version of english wikipedia.'

task = "The Metropolitan Museum of Art has a portrait in its collection with an accession number of 29.100.5. Of the consecrators and co-consecrators of this portrait's subject as a bishop, what is the name of the one who never became pope?"

task = 'go to https://browser-use.github.io/stress-tests/challenges/angularjs-form.html and go back.'
# Test: llm_screenshot_size should auto-configure to (1400, 850) for Claude Sonnet
agent = Agent(task=task, llm=llm, highlight_elements=True, flash_mode=True, use_judge=True)


async def main():
	await agent.run(max_steps=30)


if __name__ == '__main__':
	asyncio.run(main())
