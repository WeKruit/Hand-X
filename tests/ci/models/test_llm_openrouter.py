"""Test OpenRouter model button click."""

from browser_use.llm.openai.chat import ChatOpenAI
from tests.ci.models.model_test_helper import run_model_button_click_test


async def test_openrouter_grok_4_mini(httpserver):
	"""Test OpenRouter x-ai/grok-4-fast can click a button."""
	await run_model_button_click_test(
		model_class=ChatOpenAI,
		model_name='x-ai/grok-4-fast',
		api_key_env='OPENROUTER_API_KEY',
		extra_kwargs={'base_url': 'https://openrouter.ai/api/v1'},
		httpserver=httpserver,
	)
