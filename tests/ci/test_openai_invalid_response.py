"""
Regression tests for GitHub Issue #3897.

Some OpenAI-compatible proxies return HTTP 200 with an invalid body such as
`choices: null`. Browser Use should not crash by indexing `choices[0]`; it
should raise a clear ModelProviderError instead.
"""

import time

import httpx
import pytest
from pydantic import BaseModel


class _DummyOutput(BaseModel):
	ok: bool


class TestChatOpenAIInvalidResponse:
	@pytest.mark.asyncio
	async def test_choices_null_non_structured(self):
		from browser_use import ChatOpenAI
		from browser_use.llm.exceptions import ModelProviderError
		from browser_use.llm.messages import UserMessage

		def handler(request: httpx.Request) -> httpx.Response:
			body = {
				'id': 'chatcmpl-proxy-fake-12345',
				'object': 'chat.completion',
				'created': int(time.time()),
				'model': 'proxy-model',
				'choices': None,
			}
			return httpx.Response(200, json=body)

		transport = httpx.MockTransport(handler)
		async with httpx.AsyncClient(transport=transport) as http_client:
			llm = ChatOpenAI(
				model='proxy-model',
				base_url='http://proxy.local/v1',
				api_key='fake-key-not-needed',
				http_client=http_client,
				max_retries=0,
			)

			with pytest.raises(ModelProviderError) as excinfo:
				await llm.ainvoke([UserMessage(content='hi')])

		assert 'choices' in excinfo.value.message
		assert '/v1/chat/completions' in excinfo.value.message

	@pytest.mark.asyncio
	async def test_choices_null_structured(self):
		from browser_use import ChatOpenAI
		from browser_use.llm.exceptions import ModelProviderError
		from browser_use.llm.messages import UserMessage

		def handler(request: httpx.Request) -> httpx.Response:
			body = {
				'id': 'chatcmpl-proxy-fake-12345',
				'object': 'chat.completion',
				'created': int(time.time()),
				'model': 'proxy-model',
				'choices': None,
			}
			return httpx.Response(200, json=body)

		transport = httpx.MockTransport(handler)
		async with httpx.AsyncClient(transport=transport) as http_client:
			llm = ChatOpenAI(
				model='proxy-model',
				base_url='http://proxy.local/v1',
				api_key='fake-key-not-needed',
				http_client=http_client,
				max_retries=0,
				# Exercise the default structured-output path (schema injection disabled by default).
				add_schema_to_system_prompt=False,
				dont_force_structured_output=False,
			)

			with pytest.raises(ModelProviderError) as excinfo:
				await llm.ainvoke([UserMessage(content='hi')], output_format=_DummyOutput)

		assert 'choices' in excinfo.value.message
		assert '/v1/chat/completions' in excinfo.value.message
