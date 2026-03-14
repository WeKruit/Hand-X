"""LLM client factory -- routes through VALET proxy or direct API based on settings."""

from __future__ import annotations

import logging
from typing import Any

from ghosthands.config.settings import settings

logger = logging.getLogger(__name__)


def _ensure_trailing_slash(url: str) -> str:
	"""Ensure URL ends with ``/`` — required for Anthropic SDK's httpx URL joining.

	The SDK appends ``/v1/messages`` to ``base_url`` using httpx URL joining.
	Without a trailing slash, the last path segment gets *replaced* instead of
	appended.  For example:
	  ``https://host/api/proxy``  + ``/v1/messages`` → ``https://host/api/v1/messages``  ← WRONG
	  ``https://host/api/proxy/`` + ``/v1/messages`` → ``https://host/api/proxy/v1/messages``  ← CORRECT
	"""
	return url if url.endswith("/") else url + "/"


def get_anthropic_client() -> Any:
	"""Get an async Anthropic client, routed through VALET proxy if configured.

	When ``GH_LLM_PROXY_URL`` is set:
	  - ``base_url`` = proxy URL (trailing slash ensured for correct SDK path joining)
	  - ``api_key`` = runtime grant token

	When NOT set:
	  - Direct Anthropic API (standard ``ANTHROPIC_API_KEY``)
	"""
	import anthropic

	if settings.llm_proxy_url:
		proxy_url = _ensure_trailing_slash(settings.llm_proxy_url)
		logger.info(
			"llm.proxy_mode",
			extra={"proxy_url": proxy_url, "has_grant": bool(settings.llm_runtime_grant)},
		)
		return anthropic.AsyncAnthropic(
			api_key=settings.llm_runtime_grant or settings.anthropic_api_key or "dummy",
			base_url=proxy_url,
		)

	api_key = settings.anthropic_api_key
	if not api_key:
		import os

		api_key = os.environ.get("GH_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")

	return anthropic.AsyncAnthropic(api_key=api_key or None)


def get_chat_model(model: str | None = None) -> Any:
	"""Get a browser-use chat model for the agent loop.

	When proxy is configured:
	  - Always uses ``ChatAnthropic`` routed through VALET (VALET only proxies Anthropic)
	  - Model defaults to ``settings.agent_model``

	When NOT configured:
	  - Uses the appropriate provider based on model name
	"""
	model = model or settings.agent_model

	if settings.llm_proxy_url:
		from browser_use.llm.anthropic.chat import ChatAnthropic

		# When proxied, force Anthropic provider (VALET proxy only supports Anthropic format).
		# If agent_model is non-Claude, fall back to Sonnet (capable enough for agent loop).
		# Note: domhand_model (Haiku) is too weak for the agent loop — it needs reasoning.
		if model.startswith("gemini") or model.startswith("models/") or model.startswith("gpt-") or model.startswith("o"):
			proxy_model = "claude-sonnet-4-20250514"
			logger.info(
				"llm.proxy_model_override",
				extra={"original": model, "override": proxy_model},
			)
			model = proxy_model

		proxy_url = _ensure_trailing_slash(settings.llm_proxy_url)
		return ChatAnthropic(
			model=model,
			api_key=settings.llm_runtime_grant or settings.anthropic_api_key or None,
			base_url=proxy_url,
		)

	# Direct mode -- pick provider based on model name
	if model.startswith("gpt-") or model.startswith("o"):
		from browser_use.llm.openai.chat import ChatOpenAI

		return ChatOpenAI(model=model)

	if model.startswith("claude-"):
		from browser_use.llm.anthropic.chat import ChatAnthropic

		return ChatAnthropic(
			model=model,
			api_key=settings.anthropic_api_key or None,
		)

	# Default: Google Gemini
	from browser_use.llm.google.chat import ChatGoogle

	return ChatGoogle(model=model, max_output_tokens=16384)
