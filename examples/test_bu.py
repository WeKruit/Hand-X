"""
Setup:
1. Get your API key from https://cloud.browser-use.com/new-api-key
2. Set environment variable: export BROWSER_USE_API_KEY="your-key"
"""

from dotenv import load_dotenv
from lmnr import Laminar

from browser_use import Agent
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.openai.chat import ChatOpenAI

Laminar.initialize()
load_dotenv()

llm = ChatOpenAI(
	base_url='https://browseruse--qwen3-vl-8b-instruct-181125-3epoch-serve.modal.run/v1',
	api_key='aitorloveskebabs',
	model='browser-use/Qwen3-VL-8B-Instruct-181125-3epoch',
	temperature=0.6,
)

llm = ChatAnthropic(model='claude-sonnet-4-5-20250929', temperature=0.6)
# llm = ChatGoogle(model='gemini-flash-latest', temperature=0.6)

# task = """Search for "used laptops" within the price range of $300-$500. Filter by Buy now options and find an option with 8GB Ram and 500GB memory. Add it to cart. Website: https://www.ebay.com"""

# task = "Search for used laptops in website: https://ebay.com using the input action on the search bar and call done"

# task = "Go to the URL and complete the jQuery Bootstrap form by filling in all required fields and submitting. If needed, create a file. Validate that the form was filled and submitted successfully. https://browser-use.github.io/stress-tests/challenges/jquery-bootstrap-form.html"

# task = "Go to https://www.html-code-generator.com/drop-down/country-names scroll down by 1200 pixels and select the set Select Quick Access Men to be 'Grouped by names' and call done."

# task = """
# 1. Go to https://www.w3schools.com/tags/tryit.asp?filename=tryhtml_select
# 2. Get the dropdown options for Choose a Car:
# 3. Select 'Opel' from the dropdown
# """

# task = "go to https://v0-download-and-upload-text.vercel.app/ and download the text file and then upload it to the same page"


# #task = "go to https://www.betterhealth.vic.gov.au/search?q=mental+health and get the dropdown options for sort by and select the last one."

# task = "create a random txt file, go to https://v0-download-and-upload-text.vercel.app/ and upload the file"

# task = "Go to https://www.ebay.com wait for 10 secondsand scroll 850 coordinates down."

task = 'Go to the URL and complete the React Native Web form by filling in all required fields and submitting. If needed, create a file. Validate that the form was filled and submitted successfully. https://browser-use.github.io/stress-tests/challenges/react-native-web-form.html'

task = 'go tohttps://s206.q4cdn.com/479360582/files/doc_financials/2025/q3/2025q3-alphabet-earnings-release.pdf and scroll down in the PDF viewer.'

task = 'go to https://s206.q4cdn.com/479360582/files/doc_financials/2025/q3/2025q3-alphabet-earnings-release.pdf and click on the download button.'


task = 'go to https://github.com/browser-use/browser-use/tree/main/browser_use and scroll all the way down on the left sidebar.'

task = 'go to https://developer.mozilla.org/en-US/docs/Glossary/Scroll_container and scroll on the left sidebar for Glossary section.'

agent = Agent(
	task=task,
	llm=llm,
	use_judge=False,
	# use_anthropic_agent_prompt=True,
	flash_mode=True,
)
agent.run_sync()
