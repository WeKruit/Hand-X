"""
Convenient access to LLM models.

Usage:
    from browser_use import llm

    # Simple model access
    model = llm.azure_gpt_4_1_mini
    model = llm.openai_gpt_4o
    model = llm.google_gemini_2_5_pro
"""

import os
from typing import TYPE_CHECKING

from browser_use.llm.azure.chat import ChatAzureOpenAI
from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.mistral.chat import ChatMistral
from browser_use.llm.openai.chat import ChatOpenAI

if TYPE_CHECKING:
	from browser_use.llm.base import BaseChatModel

# Type stubs for IDE autocomplete
openai_gpt_4o: 'BaseChatModel'
openai_gpt_4o_mini: 'BaseChatModel'
openai_gpt_4_1_mini: 'BaseChatModel'
openai_o1: 'BaseChatModel'
openai_o1_mini: 'BaseChatModel'
openai_o1_pro: 'BaseChatModel'
openai_o3: 'BaseChatModel'
openai_o3_mini: 'BaseChatModel'
openai_o3_pro: 'BaseChatModel'
openai_o4_mini: 'BaseChatModel'
openai_gpt_5: 'BaseChatModel'
openai_gpt_5_mini: 'BaseChatModel'
openai_gpt_5_nano: 'BaseChatModel'

azure_gpt_4o: 'BaseChatModel'
azure_gpt_4o_mini: 'BaseChatModel'
azure_gpt_4_1_mini: 'BaseChatModel'
azure_o1: 'BaseChatModel'
azure_o1_mini: 'BaseChatModel'
azure_o1_pro: 'BaseChatModel'
azure_o3: 'BaseChatModel'
azure_o3_mini: 'BaseChatModel'
azure_o3_pro: 'BaseChatModel'
azure_gpt_5: 'BaseChatModel'
azure_gpt_5_mini: 'BaseChatModel'

google_gemini_2_0_flash: 'BaseChatModel'
google_gemini_2_0_pro: 'BaseChatModel'
google_gemini_2_5_pro: 'BaseChatModel'
google_gemini_2_5_flash: 'BaseChatModel'
google_gemini_2_5_flash_lite: 'BaseChatModel'
mistral_large: 'BaseChatModel'
mistral_medium: 'BaseChatModel'
mistral_small: 'BaseChatModel'
codestral: 'BaseChatModel'
pixtral_large: 'BaseChatModel'


def get_llm_by_name(model_name: str):
	"""
	Factory function to create LLM instances from string names with API keys from environment.

	Args:
	    model_name: String name like 'azure_gpt_4_1_mini', 'openai_gpt_4o', etc.

	Returns:
	    LLM instance with API keys from environment variables

	Raises:
	    ValueError: If model_name is not recognized
	"""
	if not model_name:
		raise ValueError('Model name cannot be empty')

	# Handle top-level Mistral aliases without provider prefix
	mistral_aliases = {
		'mistral_large': 'mistral-large-latest',
		'mistral_medium': 'mistral-medium-latest',
		'mistral_small': 'mistral-small-latest',
		'codestral': 'codestral-latest',
		'pixtral_large': 'pixtral-large-latest',
	}
	if model_name in mistral_aliases:
		api_key = os.getenv('MISTRAL_API_KEY')
		base_url = os.getenv('MISTRAL_BASE_URL', 'https://api.mistral.ai/v1')
		return ChatMistral(model=mistral_aliases[model_name], api_key=api_key, base_url=base_url)

	# Parse model name
	parts = model_name.split('_', 1)
	if len(parts) < 2:
		raise ValueError(f"Invalid model name format: '{model_name}'. Expected format: 'provider_model_name'")

	provider = parts[0]
	model_part = parts[1]

	# Convert underscores back to dots/dashes for actual model names
	if 'gpt_4_1_mini' in model_part:
		model = model_part.replace('gpt_4_1_mini', 'gpt-4.1-mini')
	elif 'gpt_4o_mini' in model_part:
		model = model_part.replace('gpt_4o_mini', 'gpt-4o-mini')
	elif 'gpt_4o' in model_part:
		model = model_part.replace('gpt_4o', 'gpt-4o')
	elif 'gemini_2_0' in model_part:
		model = model_part.replace('gemini_2_0', 'gemini-2.0').replace('_', '-')
	elif 'gemini_2_5' in model_part:
		model = model_part.replace('gemini_2_5', 'gemini-2.5').replace('_', '-')
	else:
		model = model_part.replace('_', '-')

	# OpenAI Models
	if provider == 'openai':
		api_key = os.getenv('OPENAI_API_KEY')
		return ChatOpenAI(model=model, api_key=api_key)

	# Azure OpenAI Models
	elif provider == 'azure':
		api_key = os.getenv('AZURE_OPENAI_KEY') or os.getenv('AZURE_OPENAI_API_KEY')
		azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
		return ChatAzureOpenAI(model=model, api_key=api_key, azure_endpoint=azure_endpoint)

	# Google Models
	elif provider == 'google':
		api_key = os.getenv('GOOGLE_API_KEY')
		return ChatGoogle(model=model, api_key=api_key)

	# Mistral Models
	elif provider == 'mistral':
		api_key = os.getenv('MISTRAL_API_KEY')
		base_url = os.getenv('MISTRAL_BASE_URL', 'https://api.mistral.ai/v1')
		mistral_map = {
			'large': 'mistral-large-latest',
			'medium': 'mistral-medium-latest',
			'small': 'mistral-small-latest',
			'codestral': 'codestral-latest',
			'pixtral-large': 'pixtral-large-latest',
		}
		resolved_model = mistral_map.get(model_part, model.replace('_', '-'))
		return ChatMistral(model=resolved_model, api_key=api_key, base_url=base_url)

	else:
		available_providers = ['openai', 'azure', 'google', 'mistral']
		raise ValueError(f"Unknown provider: '{provider}'. Available providers: {', '.join(available_providers)}")


# Pre-configured model instances (lazy loaded via __getattr__)
def __getattr__(name: str) -> 'BaseChatModel':
	"""Create model instances on demand with API keys from environment."""
	# Handle chat classes first
	if name == 'ChatOpenAI':
		return ChatOpenAI  # type: ignore
	elif name == 'ChatAzureOpenAI':
		return ChatAzureOpenAI  # type: ignore
	elif name == 'ChatGoogle':
		return ChatGoogle  # type: ignore
	elif name == 'ChatMistral':
		return ChatMistral  # type: ignore

	# Handle model instances - these are the main use case
	try:
		return get_llm_by_name(name)
	except ValueError:
		raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
	'ChatOpenAI',
	'ChatAzureOpenAI',
	'ChatGoogle',
	'ChatMistral',
	'get_llm_by_name',
	# OpenAI instances - created on demand
	'openai_gpt_4o',
	'openai_gpt_4o_mini',
	'openai_gpt_4_1_mini',
	'openai_o1',
	'openai_o1_mini',
	'openai_o1_pro',
	'openai_o3',
	'openai_o3_mini',
	'openai_o3_pro',
	'openai_o4_mini',
	'openai_gpt_5',
	'openai_gpt_5_mini',
	'openai_gpt_5_nano',
	# Azure instances - created on demand
	'azure_gpt_4o',
	'azure_gpt_4o_mini',
	'azure_gpt_4_1_mini',
	'azure_o1',
	'azure_o1_mini',
	'azure_o1_pro',
	'azure_o3',
	'azure_o3_mini',
	'azure_o3_pro',
	'azure_gpt_5',
	'azure_gpt_5_mini',
	# Google instances - created on demand
	'google_gemini_2_0_flash',
	'google_gemini_2_0_pro',
	'google_gemini_2_5_pro',
	'google_gemini_2_5_flash',
	'google_gemini_2_5_flash_lite',
	# Mistral instances - created on demand
	'mistral_large',
	'mistral_medium',
	'mistral_small',
	'codestral',
	'pixtral_large',
]
