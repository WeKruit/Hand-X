"""LLM client factory -- routes through VALET proxy or direct API based on settings."""

from __future__ import annotations

import logging
import re
from typing import Any

from ghosthands.config.settings import settings

# Match OpenAI o-series reasoning models (o1, o3, o4-mini, etc.)
# but NOT models that merely start with "o" (opus, ocr, ...).
_is_openai_o_model = re.compile(r"^o\d").match

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
	  - Gemini models → ``ChatGoogle`` with ``http_options.baseUrl`` pointed at VALET's
	    ``/gemini`` passthrough route.  The SDK appends ``/v1beta/models/{model}:generateContent``
	    and sends the runtime grant as ``x-goog-api-key``.
	  - Claude models → ``ChatAnthropic`` routed through VALET (existing behaviour).
	  - GPT/OpenAI models → overridden to Claude Sonnet (no Gemini or OpenAI proxy route).

	When NOT configured:
	  - Uses the appropriate provider based on model name (direct API keys).
	"""
	model = model or settings.agent_model

	if settings.llm_proxy_url:
		proxy_url = settings.llm_proxy_url.rstrip("/")

		# ── Gemini models → ChatGoogle via VALET /gemini passthrough ──
		if model.startswith("gemini") or model.startswith("models/"):
			from browser_use.llm.google.chat import ChatGoogle

			logger.info(
				"llm.proxy_gemini",
				extra={"model": model, "proxy_url": proxy_url},
			)
			return ChatGoogle(
				model=model,
				api_key=settings.llm_runtime_grant or settings.google_api_key or "dummy",
				http_options={"baseUrl": proxy_url + "/gemini"},
			)

		# ── GPT/OpenAI models → override to Sonnet (no OpenAI proxy route) ──
		if model.startswith("gpt-") or _is_openai_o_model(model):
			proxy_model = "claude-sonnet-4-20250514"
			logger.info(
				"llm.proxy_model_override",
				extra={"original": model, "override": proxy_model},
			)
			model = proxy_model

		# ── Claude (or overridden-to-Claude) models → ChatAnthropic via VALET ──
		from browser_use.llm.anthropic.chat import ChatAnthropic

		anthropic_proxy_url = _ensure_trailing_slash(proxy_url)
		return ChatAnthropic(
			model=model,
			api_key=settings.llm_runtime_grant or settings.anthropic_api_key or None,
			base_url=anthropic_proxy_url,
		)

	# Direct mode -- pick provider based on model name
	if model.startswith("gpt-") or _is_openai_o_model(model):
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
