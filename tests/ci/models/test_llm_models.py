"""Tests for LLM model initialization and basic functionality.

This test verifies that all supported LLM models from examples/models/ can be initialized
and execute a simple button click task. Each model is tested in parallel.
"""

import os

import pytest

from browser_use.agent.service import Agent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.azure.chat import ChatAzureOpenAI
from browser_use.llm.browser_use.chat import ChatBrowserUse
from browser_use.llm.cerebras.chat import ChatCerebras
from browser_use.llm.deepseek.chat import ChatDeepSeek
from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.groq.chat import ChatGroq
from browser_use.llm.ollama.chat import ChatOllama
from browser_use.llm.openai.chat import ChatOpenAI

# Define models to test based on the examples in examples/models/
# Each tuple is (model_class, model_name, required_api_key_env_var, extra_kwargs)
MODELS_TO_TEST = [
	# OpenAI models (from gpt-4.1.py, gpt-5-mini.py)
	pytest.param(ChatOpenAI, 'gpt-4.1-mini', 'OPENAI_API_KEY', {}, id='openai_gpt_4_1_mini'),
	pytest.param(ChatOpenAI, 'gpt-5-mini', 'OPENAI_API_KEY', {}, id='openai_gpt_5_mini'),
	# Anthropic models (from claude-4-sonnet.py)
	pytest.param(ChatAnthropic, 'claude-sonnet-4-0', 'ANTHROPIC_API_KEY', {}, id='anthropic_claude_sonnet_4_0'),
	# Google models (from gemini.py)
	pytest.param(ChatGoogle, 'gemini-flash-latest', 'GOOGLE_API_KEY', {}, id='google_gemini_flash_latest'),
	# Groq models (from llama4-groq.py)
	pytest.param(
		ChatGroq, 'meta-llama/llama-4-maverick-17b-128e-instruct', 'GROQ_API_KEY', {}, id='groq_llama_4_maverick'
	),
	# DeepSeek models (from deepseek-chat.py)
	pytest.param(
		ChatDeepSeek,
		'deepseek-chat',
		'DEEPSEEK_API_KEY',
		{'base_url': 'https://api.deepseek.com/v1'},
		id='deepseek_chat',
	),
	# Azure OpenAI (from azure_openai.py) - needs both API key and endpoint
	pytest.param(
		ChatAzureOpenAI,
		'gpt-4.1-mini',
		'AZURE_OPENAI_KEY',
		{'azure_endpoint': os.getenv('AZURE_OPENAI_ENDPOINT')},
		id='azure_gpt_4_1_mini',
	),
	# Browser Use LLM (from browser_use_llm.py)
	pytest.param(ChatBrowserUse, 'bu-latest', 'BROWSER_USE_API_KEY', {}, id='browser_use_bu_latest'),
	# Qwen via OpenAI-compatible API (from qwen.py)
	pytest.param(
		ChatOpenAI,
		'qwen-vl-max',
		'ALIBABA_CLOUD',
		{'base_url': 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'},
		id='qwen_vl_max',
	),
	# Cerebras (from cerebras_example.py) - using the model from the example
	pytest.param(
		ChatCerebras, 'qwen-3-235b-a22b-thinking-2507', 'CEREBRAS_API_KEY', {}, id='cerebras_qwen_3_235b_thinking'
	),
	# OpenRouter (from openrouter.py)
	pytest.param(
		ChatOpenAI,
		'x-ai/grok-4',
		'OPENROUTER_API_KEY',
		{'base_url': 'https://openrouter.ai/api/v1'},
		id='openrouter_grok_4',
	),
	# Ollama (from ollama.py) - local model, no API key
	pytest.param(ChatOllama, 'llama3.1:8b', None, {}, id='ollama_llama3_1_8b'),
]


@pytest.mark.parametrize('model_class,model_name,api_key_env,extra_kwargs', MODELS_TO_TEST)
async def test_llm_model_button_click(model_class, model_name, api_key_env, extra_kwargs, httpserver):
	"""Test that each LLM model can click a button.

	This test verifies:
	1. Model can be initialized with API key
	2. Agent can navigate and click a button
	3. Button click is verified by checking page state change
	4. Completes within max 2 steps
	"""
	# Skip test if API key is not available (except Ollama which is local)
	if api_key_env is not None:
		api_key = os.getenv(api_key_env)
		if not api_key:
			pytest.skip(f'{api_key_env} not set')
	else:
		api_key = None

	# Create HTML page with a button that changes page content when clicked
	html = """
	<!DOCTYPE html>
	<html>
	<head><title>Button Test</title></head>
	<body>
		<h1>Button Click Test</h1>
		<button id="test-button" onclick="document.getElementById('result').innerText='SUCCESS'">
			Click Me
		</button>
		<div id="result">NOT_CLICKED</div>
	</body>
	</html>
	"""
	httpserver.expect_request('/').respond_with_data(html, content_type='text/html')

	# Create LLM instance with extra kwargs if provided
	llm_kwargs = {'model': model_name}
	if api_key is not None:
		llm_kwargs['api_key'] = api_key
	llm_kwargs.update(extra_kwargs)
	llm = model_class(**llm_kwargs)

	# Create browser session
	browser = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,  # Use temporary directory
		)
	)

	try:
		# Start browser
		await browser.start()

		# Create agent with button click task (URL in task triggers auto-navigation)
		test_url = httpserver.url_for('/')
		agent = Agent(
			task=f'{test_url} - Click the button',
			llm=llm,
			browser_session=browser,
			max_steps=2,  # Max 2 steps as per requirements
		)

		# Run the agent
		result = await agent.run()

		# Verify task completed
		assert result is not None
		assert len(result.history) > 0

		# Verify button was clicked by checking page state across any step
		# (Don't enforce strict step count since auto-navigation and optional done actions vary by model)
		button_clicked = False
		for step in result.history:
			# Check state_message which contains browser state with page text
			if step.state_message and 'SUCCESS' in step.state_message:
				button_clicked = True
				break

		# Check if SUCCESS appears in any step (indicating button was clicked)
		assert button_clicked, 'Button was not clicked - SUCCESS not found in any page state'

	finally:
		# Clean up browser session
		await browser.kill()
		await browser.event_bus.stop(clear=True, timeout=5)


async def test_all_models_discoverable():
	"""Test that all models defined in MODELS_TO_TEST are valid.

	This is a simple sanity check that doesn't require API keys.
	"""
	assert len(MODELS_TO_TEST) > 0, 'No models defined in MODELS_TO_TEST'

	# Verify each model parameter is a tuple of the right length
	for param in MODELS_TO_TEST:
		assert len(param.values) == 4, f'Invalid model parameter: {param}'
		model_class, model_name, api_key_env, extra_kwargs = param.values

		# Verify model_class is a class
		assert callable(model_class), f'model_class {model_class} is not callable'

		# Verify model_name is a string
		assert isinstance(model_name, str), f'model_name {model_name} is not a string'

		# Verify api_key_env is a string or None (for local models like Ollama)
		assert api_key_env is None or isinstance(api_key_env, str), f'api_key_env {api_key_env} must be string or None'

		# Verify extra_kwargs is a dict
		assert isinstance(extra_kwargs, dict), f'extra_kwargs {extra_kwargs} is not a dict'
