"""Unit tests for the StagehandLayer adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghosthands.stagehand.layer import (
    StagehandLayer,
    _infer_stagehand_model,
    get_stagehand_layer,
    reset_stagehand_layer,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_stagehand_layer()
    yield
    reset_stagehand_layer()


@pytest.fixture(autouse=True)
def _stagehand_env(monkeypatch):
    """Stagehand init requires MODEL_API_KEY; tests must not hit the real API."""
    monkeypatch.setenv("MODEL_API_KEY", "test-model-key-for-stagehand-tests")
    monkeypatch.delenv("GH_STAGEHAND_DISABLE", raising=False)


def _mock_async_session(session_id="test-session-123", *, success=True):
    """Mimics ``AsyncSession`` returned from ``sessions.start()``."""
    s = MagicMock()
    s.id = session_id if success else None
    s.success = success
    s.end = AsyncMock()
    return s


def _mock_act_response(success=True, message="Done", actions=None):
    resp = MagicMock()
    resp.success = True
    resp.data = MagicMock()
    resp.data.result = MagicMock()
    resp.data.result.success = success
    resp.data.result.message = message
    resp.data.result.action_description = ""
    resp.data.result.actions = actions or []
    return resp


def _mock_observe_response(elements=None):
    resp = MagicMock()
    resp.success = True
    resp.data = MagicMock()
    items = []
    for el in elements or []:
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
    mock_client.sessions.start = AsyncMock(return_value=_mock_async_session())

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        result = await layer.ensure_started("ws://localhost:9222")

    assert result is True
    assert layer.is_available
    assert layer.session_id == "test-session-123"
    mock_client.sessions.start.assert_called_once()
    call_kw = mock_client.sessions.start.call_args.kwargs
    assert call_kw["browser"]["type"] == "local"
    assert call_kw["browser"]["cdp_url"] == "ws://localhost:9222"


@pytest.mark.asyncio
async def test_layer_start_failure():
    layer = StagehandLayer()

    mock_client = AsyncMock()
    mock_client.sessions = AsyncMock()
    resp = _mock_async_session(success=False)
    mock_client.sessions.start = AsyncMock(return_value=resp)

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        result = await layer.ensure_started("ws://localhost:9222")

    assert result is False
    assert not layer.is_available
    resp.end.assert_called_once()
    mock_client.close.assert_called_once()


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
    mock_client.sessions.start = AsyncMock(return_value=_mock_async_session())

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        await layer.ensure_started("ws://localhost:9222")
        await layer.ensure_started("ws://localhost:9222")

    mock_client.sessions.start.assert_called_once()


@pytest.mark.asyncio
async def test_act_success():
    layer = StagehandLayer()
    layer._started = True
    layer._session_id = "s1"
    mock_session = AsyncMock()
    mock_session.act = AsyncMock(return_value=_mock_act_response(success=True, message="Selected United States"))
    layer._session = mock_session

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
    mock_session = AsyncMock()
    mock_session.act = AsyncMock(side_effect=Exception("timeout"))
    layer._session = mock_session

    result = await layer.act("Do something")
    assert result.success is False
    assert "error" in result.message.lower()


@pytest.mark.asyncio
async def test_observe_success():
    layer = StagehandLayer()
    layer._started = True
    layer._session_id = "s1"
    mock_session = AsyncMock()
    mock_session.observe = AsyncMock(
        return_value=_mock_observe_response(
            [
                {"selector": "xpath=//input[@name='first_name']", "description": "First Name field", "method": "fill"},
                {"selector": "xpath=//select[@id='country']", "description": "Country dropdown", "method": "click"},
            ]
        )
    )
    layer._session = mock_session

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
    mock_session = AsyncMock()
    mock_session.end = AsyncMock()
    layer._session = mock_session
    mock_client = AsyncMock()
    mock_client.close = AsyncMock()
    layer._client = mock_client

    await layer.stop()

    assert not layer.is_available
    assert layer.session_id is None
    mock_session.end.assert_called_once()
    mock_client.close.assert_called_once()


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


def test_infer_stagehand_model_google_key_equals_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza_test_google")
    assert _infer_stagehand_model("AIza_test_google") == "google/gemini-3-flash-preview"


def test_infer_stagehand_model_openai_key_equals_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    assert _infer_stagehand_model("sk-proj-test") == "openai/gpt-5.4-mini"


def test_infer_stagehand_model_anthropic_key_equals_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert _infer_stagehand_model("sk-ant-test") == "anthropic/claude-haiku-4-5-20251001"


def test_infer_stagehand_model_aiza_prefix_without_env():
    assert _infer_stagehand_model("AIzaSy_dummy") == "google/gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_layer_start_uses_inferred_google_model(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "AIza_only_google", prepend=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza_only_google", prepend=False)
    monkeypatch.delenv("GH_STAGEHAND_MODEL", raising=False)

    layer = StagehandLayer()
    mock_client = AsyncMock()
    mock_client.sessions = AsyncMock()
    mock_client.sessions.start = AsyncMock(return_value=_mock_async_session())

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        await layer.ensure_started("ws://localhost:9222")

    call_kw = mock_client.sessions.start.call_args.kwargs
    assert call_kw["model_name"] == "google/gemini-3-flash-preview"
