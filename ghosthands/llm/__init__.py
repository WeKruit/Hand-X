"""LLM client utilities -- handles direct vs VALET proxy routing."""

from ghosthands.llm.client import get_anthropic_client, get_chat_model

__all__ = ["get_anthropic_client", "get_chat_model"]
