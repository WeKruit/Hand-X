"""Unit tests for the StagehandLayer adapter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghosthands.stagehand.layer import (
    ActResult,
    ObservedElement,
    StagehandLayer,
    reset_stagehand_layer,
    get_stagehand_layer,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_stagehand_layer()
    yield
    reset_stagehand_layer()


def _mock_start_response(session_id="test-session-123"):
    resp = MagicMock()
    resp.success = True
    resp.data = MagicMock()
    resp.data.session_id = session_id
    return resp


def _mock_act_response(success=True, message="Done", actions=None):
    resp = MagicMock()
    resp.success = True
    resp.data = MagicMock()
    resp.data.result = MagicMock()
    resp.data.result.success = success
    resp.data.result.message = message
    resp.data.result.actions = actions or []
    return resp


def _mock_observe_response(elements=None):
    resp = MagicMock()
    resp.success = True
    resp.data = MagicMock()
    items = []
    for el in (elements or []):
        item = MagicMock()
        item.selector = el.get("selector", "")
        item.description = el.get("description", "")
        item.method = el.get("method", "")
        item.arguments = el.get("arguments", [])
        item.backend_node_id = el.get("backend_node_id")
        items.append(item)
    resp.data.result = items
    return resp


@pytest.mark.asyncio
async def test_layer_not_available_before_start():
    layer = StagehandLayer()
    assert not layer.is_available
    assert layer.session_id is None


@pytest.mark.asyncio
async def test_layer_start_success():
    layer = StagehandLayer()

    mock_client = AsyncMock()
    mock_client.sessions = AsyncMock()
    mock_client.sessions.start = AsyncMock(return_value=_mock_start_response())

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        result = await layer.ensure_started("ws://localhost:9222")

    assert result is True
    assert layer.is_available
    assert layer.session_id == "test-session-123"


@pytest.mark.asyncio
async def test_layer_start_failure():
    layer = StagehandLayer()

    mock_client = AsyncMock()
    mock_client.sessions = AsyncMock()
    resp = MagicMock()
    resp.success = False
    resp.data = None
    mock_client.sessions.start = AsyncMock(return_value=resp)

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        result = await layer.ensure_started("ws://localhost:9222")

    assert result is False
    assert not layer.is_available


@pytest.mark.asyncio
async def test_layer_start_exception():
    layer = StagehandLayer()

    with patch("stagehand.AsyncStagehand", side_effect=Exception("connection refused")):
        result = await layer.ensure_started("ws://localhost:9222")

    assert result is False
    assert not layer.is_available


@pytest.mark.asyncio
async def test_layer_idempotent_start():
    layer = StagehandLayer()

    mock_client = AsyncMock()
    mock_client.sessions = AsyncMock()
    mock_client.sessions.start = AsyncMock(return_value=_mock_start_response())

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        await layer.ensure_started("ws://localhost:9222")
        await layer.ensure_started("ws://localhost:9222")

    mock_client.sessions.start.assert_called_once()


@pytest.mark.asyncio
async def test_act_success():
    layer = StagehandLayer()
    layer._started = True
    layer._session_id = "s1"
    layer._client = AsyncMock()
    layer._client.sessions.act = AsyncMock(
        return_value=_mock_act_response(success=True, message="Selected United States")
    )

    result = await layer.act("Select 'United States' in the Country dropdown")
    assert result.success is True
    assert "United States" in result.message


@pytest.mark.asyncio
async def test_act_when_not_available():
    layer = StagehandLayer()
    result = await layer.act("Click something")
    assert result.success is False
    assert "not available" in result.message


@pytest.mark.asyncio
async def test_act_exception_graceful():
    layer = StagehandLayer()
    layer._started = True
    layer._session_id = "s1"
    layer._client = AsyncMock()
    layer._client.sessions.act = AsyncMock(side_effect=Exception("timeout"))

    result = await layer.act("Do something")
    assert result.success is False
    assert "error" in result.message.lower()


@pytest.mark.asyncio
async def test_observe_success():
    layer = StagehandLayer()
    layer._started = True
    layer._session_id = "s1"
    layer._client = AsyncMock()
    layer._client.sessions.observe = AsyncMock(
        return_value=_mock_observe_response([
            {"selector": "xpath=//input[@name='first_name']", "description": "First Name field", "method": "fill"},
            {"selector": "xpath=//select[@id='country']", "description": "Country dropdown", "method": "click"},
        ])
    )

    elements = await layer.observe("Find form fields")
    assert len(elements) == 2
    assert elements[0].description == "First Name field"
    assert elements[1].selector == "xpath=//select[@id='country']"


@pytest.mark.asyncio
async def test_observe_when_not_available():
    layer = StagehandLayer()
    elements = await layer.observe()
    assert elements == []


@pytest.mark.asyncio
async def test_stop_cleans_up():
    layer = StagehandLayer()
    layer._started = True
    layer._session_id = "s1"
    mock_client = AsyncMock()
    mock_client.sessions.end = AsyncMock()
    mock_client.close = AsyncMock()
    layer._client = mock_client

    await layer.stop()

    assert not layer.is_available
    assert layer.session_id is None
    mock_client.sessions.end.assert_called_once_with("s1")


@pytest.mark.asyncio
async def test_singleton_get_stagehand_layer():
    layer1 = get_stagehand_layer()
    layer2 = get_stagehand_layer()
    assert layer1 is layer2


@pytest.mark.asyncio
async def test_singleton_reset():
    layer1 = get_stagehand_layer()
    reset_stagehand_layer()
    layer2 = get_stagehand_layer()
    assert layer1 is not layer2
