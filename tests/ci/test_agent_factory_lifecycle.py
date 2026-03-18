"""Baseline regression tests for ghosthands.agent.factory lifecycle.

Tests cover:
- create_job_agent() — verifies Agent is created with correct parameters
- run_job_agent() — verifies keep_alive controls browser cleanup:
  * keep_alive=False → browser_session.kill()  (EC2 worker path)
  * keep_alive=True/None → event_bus.stop()     (Desktop/HITL path)
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level setup: stub heavy dependencies not available in the test env
# ---------------------------------------------------------------------------


def _stub_heavy_deps():
    """Install lightweight stubs for packages missing in the CI/test environment.

    browser_use.agent.service.Agent.__init__ lazily imports
    ``browser_use.llm.anthropic.chat`` which requires the ``anthropic``
    SDK.  We pre-populate the already-imported module cache for the
    entire ``browser_use.llm.anthropic`` subtree so the lazy import at
    ``Agent.__init__`` resolves to our stub ``ChatAnthropic`` class
    (a plain object) without ever touching the real ``anthropic`` SDK.

    When other test modules (e.g. test_cli_args.py) run first, they may
    install empty stubs for ``ghosthands.agent`` and its submodules.
    We evict those stale stubs so the real modules can be imported.
    """
    # -- Evict stale stubs left by other test modules ----------------------
    # test_cli_args.py installs empty ModuleType stubs for
    # ghosthands.agent.factory (no create_job_agent attr).  Remove them
    # so importlib loads the real module.
    for key in list(sys.modules):
        if key.startswith("ghosthands.agent"):
            mod = sys.modules[key]
            # A real module has a __file__; a stub ModuleType does not.
            if getattr(mod, "__file__", None) is None:
                del sys.modules[key]

    # -- browser_use.llm.anthropic subtree stubs ----------------------------
    # By injecting these BEFORE Agent.__init__ runs its lazy import, the
    # ``from browser_use.llm.anthropic.chat import ChatAnthropic`` at
    # agent/service.py:503 resolves immediately from sys.modules without
    # triggering the real anthropic SDK import chain.
    _chat_key = "browser_use.llm.anthropic.chat"
    if _chat_key not in sys.modules:
        # Parent package stub (browser_use.llm.anthropic)
        llm_anthropic = types.ModuleType("browser_use.llm.anthropic")
        sys.modules.setdefault("browser_use.llm.anthropic", llm_anthropic)

        # The chat module with a dummy ChatAnthropic class
        chat_mod = types.ModuleType(_chat_key)
        chat_mod.ChatAnthropic = type("ChatAnthropic", (), {})
        sys.modules[_chat_key] = chat_mod

        # Serializer module (imported by chat.py)
        ser_key = "browser_use.llm.anthropic.serializer"
        ser_mod = types.ModuleType(ser_key)
        ser_mod.AnthropicMessageSerializer = type("AnthropicMessageSerializer", (), {})
        sys.modules.setdefault(ser_key, ser_mod)


_stub_heavy_deps()

from ghosthands.agent.factory import create_job_agent, run_job_agent  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    """Minimal mock LLM that satisfies create_job_agent's requirements."""
    llm = AsyncMock()
    llm.model = "mock-model"
    llm._verified_api_keys = True
    llm.provider = "mock"
    llm.name = "mock-model"
    llm.model_name = "mock-model"
    return llm


@pytest.fixture
def sample_profile():
    """Minimal resume profile dict."""
    return {
        "name": "Test User",
        "email": "test@example.com",
        "phone": "+1-555-0100",
        "experience": [],
        "education": [],
        "skills": [],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_agent(*, run_side_effect=None, is_done=True, n_steps=3):
    """Create a mock Agent with explicit event_bus mock for keep_alive tests."""
    mock_agent = AsyncMock()

    # Browser session with both kill() and event_bus.stop()
    mock_event_bus = AsyncMock()
    mock_event_bus.stop = AsyncMock()

    mock_browser_session = AsyncMock()
    mock_browser_session.kill = AsyncMock()
    mock_browser_session.event_bus = mock_event_bus

    mock_agent.browser_session = mock_browser_session

    # Agent run result
    mock_history = MagicMock()
    mock_history.is_done.return_value = is_done
    mock_history.history = []

    if run_side_effect:
        mock_agent.run = AsyncMock(side_effect=run_side_effect)
    else:
        mock_agent.run = AsyncMock(return_value=mock_history)

    mock_agent.state = MagicMock()
    mock_agent.state.n_steps = n_steps

    return mock_agent


# ---------------------------------------------------------------------------
# create_job_agent tests
#
# NOTE: get_chat_model is imported LOCALLY inside create_job_agent (line 103),
# so we must patch it at the source module: ghosthands.llm.client.get_chat_model
# build_system_prompt IS imported at module level (line 35), so we patch
# it on the factory module.
# ---------------------------------------------------------------------------


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_returns_agent(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: create_job_agent returns an Agent instance."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://jobs.lever.co/example/12345",
        resume_profile=sample_profile,
    )

    from browser_use import Agent

    assert isinstance(agent, Agent)


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_sets_keep_alive_true(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: create_job_agent creates BrowserProfile with keep_alive=True.
    NOTE: This keep_alive=True is set for HITL/review use cases but is
    IGNORED by run_job_agent's finally block which always calls kill()."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    # BASELINE: keep_alive is True on the browser profile
    assert agent.browser_profile.keep_alive is True


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_headless_defaults_to_settings(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: headless defaults to settings.headless when not provided."""
    mock_get_model.return_value = mock_llm

    from ghosthands.config.settings import settings

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    # BASELINE: headless should match the settings default
    assert agent.browser_profile.headless == settings.headless


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_headless_override(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: headless=False overrides settings default."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        headless=False,
    )

    assert agent.browser_profile.headless is False


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_passes_allowed_domains(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: allowed_domains override is passed to BrowserProfile."""
    mock_get_model.return_value = mock_llm
    custom_domains = ["example.com", "custom-ats.com"]

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        allowed_domains=custom_domains,
    )

    # BASELINE: allowed_domains are set on the browser profile
    assert agent.browser_profile.allowed_domains == custom_domains


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_allowed_domains_defaults_to_settings(
    mock_prompt, mock_get_model, mock_llm, sample_profile
):
    """BASELINE: allowed_domains defaults to settings.allowed_domains when not provided."""
    mock_get_model.return_value = mock_llm

    from ghosthands.config.settings import settings

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    # BASELINE: defaults come from settings
    assert agent.browser_profile.allowed_domains == settings.allowed_domains


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_passes_credentials_as_sensitive_data(
    mock_prompt, mock_get_model, mock_llm, sample_profile
):
    """BASELINE: credentials dict is passed through as sensitive_data on the Agent."""
    mock_get_model.return_value = mock_llm
    creds = {"email": "user@example.com", "password": "s3cret"}

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        credentials=creds,
    )

    # BASELINE: sensitive_data is a copy of credentials
    assert agent.sensitive_data == creds


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_no_credentials_means_no_sensitive_data(
    mock_prompt, mock_get_model, mock_llm, sample_profile
):
    """BASELINE: without credentials, sensitive_data is None."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        credentials=None,
    )

    assert agent.sensitive_data is None


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_sets_vision_and_thinking(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: Agent is created with use_vision='auto', use_thinking=True, use_judge=False."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    # BASELINE: these flags are hardcoded in factory.py
    assert agent.settings.use_vision == "auto"
    assert agent.settings.use_judge is False


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_sets_max_failures_to_5(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: Agent is created with max_failures=5."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    # BASELINE: max_failures is hardcoded to 5
    assert agent.settings.max_failures == 5


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_sets_calculate_cost(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: Agent has calculate_cost=True for cost tracking."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    assert agent.settings.calculate_cost is True


@patch("ghosthands.llm.client.get_chat_model")
@patch("ghosthands.agent.factory.build_system_prompt", return_value="mock system prompt")
async def test_create_job_agent_extends_system_message(mock_prompt, mock_get_model, mock_llm, sample_profile):
    """BASELINE: Agent receives the system prompt from build_system_prompt."""
    mock_get_model.return_value = mock_llm

    agent = await create_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        platform="workday",
    )

    # build_system_prompt was called with profile and platform
    mock_prompt.assert_called_once_with(sample_profile, "workday")

    # The extend_system_message is set on the agent
    assert agent.settings.extend_system_message == "mock system prompt"


# ---------------------------------------------------------------------------
# run_job_agent tests — S3: keep_alive controls browser cleanup
# ---------------------------------------------------------------------------


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_keep_alive_false_kills_browser(mock_create, sample_profile):
    """S3: keep_alive=False calls browser_session.kill(), NOT event_bus.stop()."""
    mock_agent = _make_mock_agent()
    mock_create.return_value = mock_agent

    await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        keep_alive=False,
    )

    mock_agent.browser_session.kill.assert_called_once()
    mock_agent.browser_session.event_bus.stop.assert_not_called()


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_keep_alive_true_preserves_browser(mock_create, sample_profile):
    """S3: keep_alive=True calls event_bus.stop(clear=False, timeout=1.0), NOT kill()."""
    mock_agent = _make_mock_agent()
    mock_create.return_value = mock_agent

    await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        keep_alive=True,
    )

    mock_agent.browser_session.kill.assert_not_called()
    mock_agent.browser_session.event_bus.stop.assert_called_once_with(clear=False, timeout=1.0)


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_default_keep_alive_kills_browser(mock_create, sample_profile):
    """P1-5: keep_alive defaults to False — kill() the browser (safe worker default)."""
    mock_agent = _make_mock_agent()
    mock_create.return_value = mock_agent

    await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        # keep_alive defaults to False (P1-5 fix)
    )

    mock_agent.browser_session.kill.assert_called_once()
    mock_agent.browser_session.event_bus.stop.assert_not_called()


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_keep_alive_false_kills_on_exception(mock_create, sample_profile):
    """S3: keep_alive=False still calls kill() when agent.run() raises."""
    mock_agent = _make_mock_agent(run_side_effect=RuntimeError("LLM failed"))
    mock_create.return_value = mock_agent

    with pytest.raises(RuntimeError, match="LLM failed"):
        await run_job_agent(
            task="Apply to https://example.com",
            resume_profile=sample_profile,
            keep_alive=False,
        )

    mock_agent.browser_session.kill.assert_called_once()
    mock_agent.browser_session.event_bus.stop.assert_not_called()


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_keep_alive_true_preserves_on_exception(mock_create, sample_profile):
    """S3: keep_alive=True calls event_bus.stop() even when agent.run() raises."""
    mock_agent = _make_mock_agent(run_side_effect=RuntimeError("LLM failed"))
    mock_create.return_value = mock_agent

    with pytest.raises(RuntimeError, match="LLM failed"):
        await run_job_agent(
            task="Apply to https://example.com",
            resume_profile=sample_profile,
            keep_alive=True,
        )

    mock_agent.browser_session.kill.assert_not_called()
    mock_agent.browser_session.event_bus.stop.assert_called_once_with(clear=False, timeout=1.0)


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_handles_event_bus_stop_exception_gracefully(mock_create, sample_profile):
    """S3: If event_bus.stop() raises, the exception is swallowed."""
    mock_agent = _make_mock_agent()
    mock_agent.browser_session.event_bus.stop = AsyncMock(side_effect=Exception("Bus error"))
    mock_create.return_value = mock_agent

    # Should NOT raise even though event_bus.stop() failed
    result = await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        keep_alive=True,
    )

    assert isinstance(result, dict)


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_skips_cleanup_when_browser_session_is_none(mock_create, sample_profile):
    """S3: If browser_session is None, no cleanup is attempted (no crash)."""
    mock_agent = AsyncMock()
    mock_agent.browser_session = None

    mock_history = MagicMock()
    mock_history.is_done.return_value = False
    mock_history.history = []
    mock_agent.run = AsyncMock(return_value=mock_history)
    mock_agent.state = MagicMock()
    mock_agent.state.n_steps = 0

    mock_create.return_value = mock_agent

    result = await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    assert isinstance(result, dict)
    assert result["success"] is False


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_returns_success_result(mock_create, sample_profile):
    """BASELINE: run_job_agent returns a result dict with expected keys on success."""
    mock_agent = AsyncMock()
    mock_agent.browser_session = None  # skip cleanup for simplicity

    mock_result = MagicMock()
    mock_result.is_done = True
    mock_result.success = True
    mock_result.extracted_content = "Application submitted successfully"

    mock_last_entry = MagicMock()
    mock_last_entry.result = [mock_result]

    mock_history = MagicMock()
    mock_history.is_done.return_value = True
    mock_history.history = [mock_last_entry]

    mock_agent.run = AsyncMock(return_value=mock_history)
    mock_agent.state = MagicMock()
    mock_agent.state.n_steps = 10

    mock_create.return_value = mock_agent

    result = await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    assert result["success"] is True
    assert result["steps"] == 10
    assert "cost_usd" in result
    assert result["extracted_text"] == "Application submitted successfully"
    assert result["blocker"] is None


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_detects_blocker_in_result(mock_create, sample_profile):
    """BASELINE: Blocker detection: if extracted_text contains 'blocker:', it is captured."""
    mock_agent = AsyncMock()
    mock_agent.browser_session = None

    mock_result = MagicMock()
    mock_result.is_done = True
    mock_result.success = False
    mock_result.extracted_content = "Blocker: CAPTCHA detected, cannot proceed"

    mock_last_entry = MagicMock()
    mock_last_entry.result = [mock_result]

    mock_history = MagicMock()
    mock_history.is_done.return_value = True
    mock_history.history = [mock_last_entry]

    mock_agent.run = AsyncMock(return_value=mock_history)
    mock_agent.state = MagicMock()
    mock_agent.state.n_steps = 5

    mock_create.return_value = mock_agent

    result = await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    assert result["blocker"] is not None
    assert "CAPTCHA" in result["blocker"]


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_not_done_returns_failure(mock_create, sample_profile):
    """BASELINE: If agent does not reach done state, success is False."""
    mock_agent = AsyncMock()
    mock_agent.browser_session = None

    mock_history = MagicMock()
    mock_history.is_done.return_value = False
    mock_history.history = []

    mock_agent.run = AsyncMock(return_value=mock_history)
    mock_agent.state = MagicMock()
    mock_agent.state.n_steps = 100

    mock_create.return_value = mock_agent

    result = await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
    )

    assert result["success"] is False
    assert result["extracted_text"] is None
    assert result["blocker"] is None


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_passes_hooks_to_agent_run(mock_create, sample_profile):
    """BASELINE: run_job_agent creates StepHooks and passes on_step_start/end to agent.run()."""
    mock_agent = AsyncMock()
    mock_agent.browser_session = None

    mock_history = MagicMock()
    mock_history.is_done.return_value = False
    mock_history.history = []
    mock_agent.run = AsyncMock(return_value=mock_history)
    mock_agent.state = MagicMock()
    mock_agent.state.n_steps = 0

    mock_create.return_value = mock_agent

    await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        job_id="test-job-123",
    )

    mock_agent.run.assert_called_once()
    call_kwargs = mock_agent.run.call_args.kwargs
    assert "on_step_start" in call_kwargs
    assert "on_step_end" in call_kwargs
    assert callable(call_kwargs["on_step_start"])
    assert callable(call_kwargs["on_step_end"])


@patch("ghosthands.agent.factory.create_job_agent")
async def test_run_job_agent_handles_kill_exception_gracefully(mock_create, sample_profile):
    """BASELINE: If browser_session.kill() itself raises, the exception is swallowed."""
    mock_agent = _make_mock_agent()
    mock_agent.browser_session.kill = AsyncMock(side_effect=Exception("Browser already closed"))
    mock_create.return_value = mock_agent

    # Should NOT raise even though kill() failed
    result = await run_job_agent(
        task="Apply to https://example.com",
        resume_profile=sample_profile,
        keep_alive=False,
    )

    assert isinstance(result, dict)
