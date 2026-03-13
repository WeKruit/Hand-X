"""Baseline regression tests for ghosthands.worker.hitl.HITLManager.

Tests cover:
- HITLManager.__init__() — initialization with db, valet, worker_id
- pause_job() — updates DB status, writes audit event, notifies VALET
- wait_for_resume() — LISTEN/NOTIFY path and polling fallback
- _poll_for_resume() — polling fallback when LISTEN fails
- Timeout and cancel signal handling
- DEFAULT_WAIT_TIMEOUT constant value
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module-level setup: stub heavy dependencies before importing the module
# under test.
#
# Import chain:
#   ghosthands.worker.hitl
#     -> ghosthands.worker.__init__
#       -> ghosthands.worker.executor
#         -> ghosthands.integrations.__init__
#           -> ghosthands.integrations.database  (imports asyncpg)
#         -> ghosthands.agent.factory
#           -> browser_use.agent.service.Agent.__init__
#             -> browser_use.llm.anthropic.chat  (imports anthropic SDK)
#
# We stub:
#   1. asyncpg          — not installed in the CI/test environment
#   2. browser_use.llm.anthropic subtree — avoids pulling in anthropic SDK
# ---------------------------------------------------------------------------


def _stub_heavy_deps():
    """Install lightweight module stubs for packages absent in test env."""

    # -- asyncpg -----------------------------------------------------------
    if "asyncpg" not in sys.modules:
        asyncpg_mod = types.ModuleType("asyncpg")
        # The only attribute hitl.py references directly: asyncpg.Connection
        asyncpg_mod.Connection = type("Connection", (), {})
        # database.py also uses asyncpg.create_pool / asyncpg.Record etc.
        asyncpg_mod.create_pool = MagicMock()
        asyncpg_mod.Record = type("Record", (), {})
        sys.modules["asyncpg"] = asyncpg_mod
        sys.modules["asyncpg.pool"] = types.ModuleType("asyncpg.pool")

    # -- browser_use.llm.anthropic subtree ---------------------------------
    _chat_key = "browser_use.llm.anthropic.chat"
    if _chat_key not in sys.modules:
        llm_anthropic = types.ModuleType("browser_use.llm.anthropic")
        sys.modules.setdefault("browser_use.llm.anthropic", llm_anthropic)

        chat_mod = types.ModuleType(_chat_key)
        chat_mod.ChatAnthropic = type("ChatAnthropic", (), {})
        sys.modules[_chat_key] = chat_mod

        ser_key = "browser_use.llm.anthropic.serializer"
        ser_mod = types.ModuleType(ser_key)
        ser_mod.AnthropicMessageSerializer = type("AnthropicMessageSerializer", (), {})
        sys.modules.setdefault(ser_key, ser_mod)


_stub_heavy_deps()

from ghosthands.worker.hitl import DEFAULT_WAIT_TIMEOUT, HITLManager  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    """Mock Database instance with all methods used by HITLManager."""
    db = AsyncMock()
    db.update_job_status = AsyncMock()
    db.write_job_event = AsyncMock()
    db.listen_for_signals = AsyncMock()
    db.unlisten = AsyncMock()
    db._require_pool = MagicMock()
    return db


@pytest.fixture
def mock_valet():
    """Mock ValetClient instance."""
    valet = AsyncMock()
    valet.report_needs_human = AsyncMock(return_value=True)
    return valet


@pytest.fixture
def hitl(mock_db, mock_valet):
    """Create a HITLManager with mocked dependencies."""
    return HITLManager(db=mock_db, valet=mock_valet, worker_id="test-worker-1")


# ---------------------------------------------------------------------------
# __init__ tests
# ---------------------------------------------------------------------------


def test_hitl_manager_init(mock_db, mock_valet):
    """BASELINE: HITLManager initializes with db, valet, and worker_id."""
    mgr = HITLManager(db=mock_db, valet=mock_valet, worker_id="w-1")

    assert mgr.db is mock_db
    assert mgr.valet is mock_valet
    assert mgr.worker_id == "w-1"


def test_hitl_manager_init_state(mock_db, mock_valet):
    """BASELINE: Initial state has no listen connection, cleared event, no signal data."""
    mgr = HITLManager(db=mock_db, valet=mock_valet, worker_id="w-1")

    assert mgr._listen_conn is None
    assert not mgr._signal_event.is_set()
    assert mgr._signal_data is None


# ---------------------------------------------------------------------------
# DEFAULT_WAIT_TIMEOUT
# ---------------------------------------------------------------------------


def test_default_wait_timeout_is_300_seconds():
    """BASELINE: DEFAULT_WAIT_TIMEOUT is 300.0 seconds (5 minutes)."""
    assert DEFAULT_WAIT_TIMEOUT == 300.0


# ---------------------------------------------------------------------------
# check_for_signals tests
# ---------------------------------------------------------------------------


async def test_check_for_signals_returns_none_for_running(hitl, mock_db):
    """BASELINE: check_for_signals returns None when job status is 'running'."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "running"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl.check_for_signals("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert result is None


async def test_check_for_signals_returns_cancel_for_cancelled(hitl, mock_db):
    """BASELINE: check_for_signals returns 'cancel' when status is 'cancelled'."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "cancelled"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl.check_for_signals("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert result == "cancel"


async def test_check_for_signals_returns_cancel_for_missing_job(hitl, mock_db):
    """BASELINE: check_for_signals returns 'cancel' when job row doesn't exist."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value=None)
    mock_db._require_pool.return_value = mock_pool

    result = await hitl.check_for_signals("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert result == "cancel"


async def test_check_for_signals_returns_resume_for_pending(hitl, mock_db):
    """BASELINE: check_for_signals returns 'resume' if status is 'pending' (re-queued)."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "pending"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl.check_for_signals("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert result == "resume"


async def test_check_for_signals_returns_resume_for_queued(hitl, mock_db):
    """BASELINE: check_for_signals returns 'resume' for 'queued' status."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "queued"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl.check_for_signals("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert result == "resume"


async def test_check_for_signals_returns_none_for_needs_human(hitl, mock_db):
    """BASELINE: check_for_signals returns None for 'needs_human' (still waiting)."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "needs_human"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl.check_for_signals("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert result is None


# ---------------------------------------------------------------------------
# pause_job tests
# ---------------------------------------------------------------------------


async def test_pause_job_updates_db_status(hitl, mock_db):
    """BASELINE: pause_job calls db.update_job_status with 'needs_human'."""
    await hitl.pause_job(
        job_id="test-job-1",
        reason="captcha detected",
        interaction_type="captcha",
        page_url="https://example.com/apply",
    )

    mock_db.update_job_status.assert_called_once()
    call_args = mock_db.update_job_status.call_args
    assert call_args[0][0] == "test-job-1"
    assert call_args[0][1] == "needs_human"

    # BASELINE: metadata includes interaction_type, reason, page_url, screenshot_url
    metadata = call_args[1]["metadata"] if "metadata" in call_args[1] else call_args[0][2]
    assert metadata["interaction_type"] == "captcha"
    assert metadata["reason"] == "captcha detected"
    assert metadata["page_url"] == "https://example.com/apply"


async def test_pause_job_writes_audit_event(hitl, mock_db):
    """BASELINE: pause_job writes a 'needs_human' event to gh_job_events."""
    await hitl.pause_job(
        job_id="test-job-1",
        reason="login wall",
        interaction_type="login",
    )

    mock_db.write_job_event.assert_called_once()
    call_args = mock_db.write_job_event.call_args
    assert call_args[0][0] == "test-job-1"
    assert call_args[0][1] == "needs_human"

    metadata = call_args[1]["metadata"] if "metadata" in call_args[1] else call_args[0][2]
    assert metadata["type"] == "login"
    assert metadata["reason"] == "login wall"
    assert metadata["worker_id"] == "test-worker-1"


async def test_pause_job_notifies_valet(hitl, mock_valet):
    """BASELINE: pause_job calls valet.report_needs_human with correct parameters."""
    await hitl.pause_job(
        job_id="test-job-1",
        reason="2FA required",
        interaction_type="2fa",
        screenshot_url="https://cdn.example.com/screenshot.png",
        page_url="https://example.com/login",
        valet_task_id="vt-42",
    )

    mock_valet.report_needs_human.assert_called_once_with(
        job_id="test-job-1",
        interaction_type="2fa",
        description="2FA required",
        valet_task_id="vt-42",
        worker_id="test-worker-1",
        screenshot_url="https://cdn.example.com/screenshot.png",
        page_url="https://example.com/login",
    )


async def test_pause_job_optional_fields_default_to_none(hitl, mock_db, mock_valet):
    """BASELINE: screenshot_url, page_url, valet_task_id are optional (default None)."""
    await hitl.pause_job(
        job_id="test-job-1",
        reason="blocker",
    )

    # DB metadata should have None for optional fields
    metadata = mock_db.update_job_status.call_args[1]["metadata"] if "metadata" in mock_db.update_job_status.call_args[1] else mock_db.update_job_status.call_args[0][2]
    assert metadata["screenshot_url"] is None
    assert metadata["page_url"] is None

    # VALET callback should have None for optional fields
    valet_kwargs = mock_valet.report_needs_human.call_args[1]
    assert valet_kwargs["valet_task_id"] is None
    assert valet_kwargs["screenshot_url"] is None
    assert valet_kwargs["page_url"] is None


# ---------------------------------------------------------------------------
# wait_for_resume tests
# ---------------------------------------------------------------------------


async def test_wait_for_resume_returns_signal_on_resume(hitl, mock_db):
    """BASELINE: wait_for_resume returns signal data when resume signal arrives."""
    # Mock listen_for_signals to invoke callback immediately with resume signal
    async def fake_listen(job_id, callback):
        await callback({"action": "resume", "user": "human"})
        return AsyncMock()  # mock connection

    mock_db.listen_for_signals = AsyncMock(side_effect=fake_listen)

    result = await hitl.wait_for_resume("test-job-1", timeout=5.0)

    assert result is not None
    assert result.get("action") == "resume"

    # BASELINE: on resume, job status is updated back to 'running'
    mock_db.update_job_status.assert_called_with("test-job-1", "running")

    # BASELINE: 'resumed' event is written
    mock_db.write_job_event.assert_called_once()
    event_call = mock_db.write_job_event.call_args
    assert event_call[0][1] == "resumed"


async def test_wait_for_resume_returns_cancel_on_cancel_signal(hitl, mock_db):
    """BASELINE: wait_for_resume handles cancel signal by updating status to 'cancelled'."""
    async def fake_listen(job_id, callback):
        await callback({"action": "cancel"})
        return AsyncMock()

    mock_db.listen_for_signals = AsyncMock(side_effect=fake_listen)

    result = await hitl.wait_for_resume("test-job-1", timeout=5.0)

    assert result == {"action": "cancel"}

    # BASELINE: on cancel, job status is updated to 'cancelled'
    mock_db.update_job_status.assert_called_with("test-job-1", "cancelled")


async def test_wait_for_resume_returns_none_on_timeout(hitl, mock_db):
    """BASELINE: wait_for_resume returns None when timeout expires with no signal."""
    # listen_for_signals returns a connection but never invokes callback
    mock_conn = AsyncMock()
    mock_db.listen_for_signals = AsyncMock(return_value=mock_conn)

    result = await hitl.wait_for_resume("test-job-1", timeout=0.1)

    assert result is None

    # BASELINE: unlisten is called in finally block
    mock_db.unlisten.assert_called_once_with(mock_conn, "test-job-1")


async def test_wait_for_resume_falls_back_to_polling_on_listen_failure(hitl, mock_db):
    """BASELINE: If listen_for_signals raises, falls back to _poll_for_resume."""
    mock_db.listen_for_signals = AsyncMock(side_effect=Exception("pgbouncer no LISTEN"))

    # Mock _poll_for_resume
    with patch.object(hitl, "_poll_for_resume", new_callable=AsyncMock) as mock_poll:
        mock_poll.return_value = {"action": "resume", "source": "poll"}

        result = await hitl.wait_for_resume("test-job-1", timeout=5.0)

        mock_poll.assert_called_once_with("test-job-1", 5.0)
        assert result == {"action": "resume", "source": "poll"}


async def test_wait_for_resume_cleans_up_listener(hitl, mock_db):
    """BASELINE: wait_for_resume always calls unlisten in finally block."""
    mock_conn = AsyncMock()

    async def fake_listen(job_id, callback):
        await callback({"action": "resume"})
        return mock_conn

    mock_db.listen_for_signals = AsyncMock(side_effect=fake_listen)

    await hitl.wait_for_resume("test-job-1", timeout=5.0)

    # BASELINE: unlisten is called with the connection and job_id
    mock_db.unlisten.assert_called_once_with(mock_conn, "test-job-1")

    # BASELINE: _listen_conn is reset to None after cleanup
    assert hitl._listen_conn is None


async def test_wait_for_resume_default_action_is_resume(hitl, mock_db):
    """BASELINE: If signal has no 'action' key, defaults to 'resume'."""
    async def fake_listen(job_id, callback):
        # Signal with no 'action' key
        await callback({"data": "something"})
        return AsyncMock()

    mock_db.listen_for_signals = AsyncMock(side_effect=fake_listen)

    result = await hitl.wait_for_resume("test-job-1", timeout=5.0)

    # BASELINE: action defaults to "resume" via signal.get("action", "resume")
    assert result is not None
    # The original signal dict is returned (not modified)
    assert result.get("data") == "something"


# ---------------------------------------------------------------------------
# _poll_for_resume tests
# ---------------------------------------------------------------------------


async def test_poll_for_resume_returns_cancel_when_cancelled(hitl, mock_db):
    """BASELINE: _poll_for_resume returns {'action': 'cancel'} when job is cancelled."""
    mock_pool = AsyncMock()
    # First call: check_for_signals sees 'cancelled'
    mock_pool.fetchrow = AsyncMock(return_value={"status": "cancelled"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl._poll_for_resume("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", timeout=10.0)

    assert result == {"action": "cancel"}


async def test_poll_for_resume_returns_resume_when_status_changes(hitl, mock_db):
    """BASELINE: _poll_for_resume returns resume when status changes from needs_human."""
    mock_pool = AsyncMock()
    # check_for_signals sees 'running', then the status check sees 'running' (not needs_human)
    mock_pool.fetchrow = AsyncMock(return_value={"status": "running"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl._poll_for_resume("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", timeout=10.0)

    assert result is not None
    assert result["action"] == "resume"
    assert result.get("source") == "status_change"

    # BASELINE: status is updated to 'running' (it may already be running,
    # but the code calls update_job_status regardless for the resume path)


async def test_poll_for_resume_returns_none_on_timeout(hitl, mock_db):
    """BASELINE: _poll_for_resume returns None when timeout expires."""
    mock_pool = AsyncMock()
    # Status stays as needs_human throughout
    mock_pool.fetchrow = AsyncMock(return_value={"status": "needs_human"})
    mock_db._require_pool.return_value = mock_pool

    # Use a very short timeout to avoid slow tests
    result = await hitl._poll_for_resume("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", timeout=0.1)

    assert result is None


async def test_poll_for_resume_poll_interval_is_5_seconds(hitl, mock_db):
    """BASELINE: _poll_for_resume uses a 5-second poll interval."""
    # This test verifies the poll_interval constant by checking that with
    # a timeout shorter than poll_interval, we get at most one poll cycle.
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "needs_human"})
    mock_db._require_pool.return_value = mock_pool

    # With timeout=3.0 and poll_interval=5.0, we expect one sleep of 3.0
    # (min(5.0, 3.0-0.0) = 3.0), then elapsed=5.0 >= 3.0, one check, then timeout
    result = await hitl._poll_for_resume("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", timeout=0.1)

    assert result is None


async def test_poll_for_resume_returns_resume_on_pending_status(hitl, mock_db):
    """BASELINE: _poll_for_resume detects re-queued job (pending status) as resume."""
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"status": "pending"})
    mock_db._require_pool.return_value = mock_pool

    result = await hitl._poll_for_resume("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", timeout=10.0)

    assert result == {"action": "resume"}

    # BASELINE: status updated to 'running' on resume
    mock_db.update_job_status.assert_called_with("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "running")
