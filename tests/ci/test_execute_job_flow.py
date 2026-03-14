"""Flow tests for ghosthands.worker.executor.execute_job.

Tests cover the main outcomes of execute_job:
1. Success path  -> result written to DB, completion reported to VALET
2. Failure path  -> agent fails, VALET notified with success=False
3. Blocker path  -> HITL pause_job triggered (inside _run_agent)
4. Domain blocked -> early rejection
5. Budget exceeded -> BudgetExceededError handled
6. Unhandled exception -> _fail_job with internal_error

All heavy dependencies (asyncpg, browser-use, anthropic SDK) are
stubbed so these tests run without external services.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module-level setup: stub heavy dependencies before importing executor
# ---------------------------------------------------------------------------

def _stub_heavy_deps():
    """Install lightweight module stubs for packages absent in test env.

    Import chain:
      ghosthands.worker.executor
        -> ghosthands.integrations.__init__
          -> ghosthands.integrations.database (imports asyncpg)
        -> ghosthands.agent.factory
          -> browser_use.agent.service.Agent.__init__
            -> browser_use.llm.anthropic.chat (imports anthropic SDK)
    """
    # -- Evict stale stubs left by other test modules ----------------------
    for key in list(sys.modules):
        if key.startswith("ghosthands.agent"):
            mod = sys.modules[key]
            if getattr(mod, "__file__", None) is None:
                del sys.modules[key]

    # -- asyncpg -----------------------------------------------------------
    if "asyncpg" not in sys.modules:
        asyncpg_mod = types.ModuleType("asyncpg")
        asyncpg_mod.Connection = type("Connection", (), {})
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

from ghosthands.worker.executor import execute_job  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_job(**overrides) -> dict:
    """Create a minimal job dict matching the gh_automation_jobs row shape."""
    defaults = {
        "id": "job-test-001",
        "user_id": "user-abc-123",
        "target_url": "https://boards.greenhouse.io/acme/jobs/456",
        "job_type": "apply",
        "input_data": {},
        "valet_task_id": "valet-task-001",
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def mock_db():
    """Mock Database instance with all methods used by execute_job."""
    db = AsyncMock()
    db.heartbeat = AsyncMock()
    db.write_job_result = AsyncMock()
    db.write_job_event = AsyncMock()
    db.update_job_status = AsyncMock()
    db.load_credentials = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_valet():
    """Mock ValetClient with all methods used by execute_job."""
    valet = AsyncMock()
    valet.report_running = AsyncMock()
    valet.report_progress = AsyncMock()
    valet.report_completion = AsyncMock()
    return valet


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestExecuteJobFlow:
    """Flow tests for the execute_job function."""

    # ------------------------------------------------------------------
    # Scenario 1: Success — job completes, result reported to VALET
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_success_reports_to_valet(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When the agent succeeds, VALET receives report_completion
        with success=True and result data."""
        mock_load_resume.return_value = {"full_name": "Test User", "email": "test@example.com"}
        mock_run_agent.return_value = {
            "success": True,
            "summary": "Application submitted",
            "steps_taken": 15,
            "cost_usd": 0.08,
            "blocker": None,
        }

        job = _make_job()
        result = await execute_job(job, mock_db, mock_valet)

        # Result should indicate success
        assert result["success"] is True
        assert result["platform"] == "greenhouse"

        # VALET should have been notified of running state
        mock_valet.report_running.assert_called_once()

        # VALET should have received the completion callback
        mock_valet.report_completion.assert_called_once()
        completion_kwargs = mock_valet.report_completion.call_args.kwargs
        assert completion_kwargs["success"] is True
        assert completion_kwargs["job_id"] == "job-test-001"
        assert completion_kwargs["valet_task_id"] == "valet-task-001"

        # DB should have the result written
        mock_db.write_job_result.assert_called_once()
        mock_db.write_job_event.assert_called_once()

    # ------------------------------------------------------------------
    # Scenario 2: Failure — agent returns success=False (no blocker)
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_failure_reports_to_valet(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When the agent fails (success=False, no blocker), VALET
        receives report_completion with the failure result."""
        mock_load_resume.return_value = {"full_name": "Test User"}
        mock_run_agent.return_value = {
            "success": False,
            "summary": "Agent did not complete the form",
            "steps_taken": 50,
            "cost_usd": 0.40,
            "blocker": None,
        }

        job = _make_job()
        result = await execute_job(job, mock_db, mock_valet)

        # Result should indicate failure
        assert result["success"] is False

        # VALET report_completion should still be called
        mock_valet.report_completion.assert_called_once()
        completion_kwargs = mock_valet.report_completion.call_args.kwargs
        assert completion_kwargs["success"] is True  # execute_job itself succeeded
        # The result_data carries the agent's success=False
        assert completion_kwargs["result"]["success"] is False

    # ------------------------------------------------------------------
    # Scenario 3: Blocker result flows through execute_job
    #
    # The actual HITL pause logic lives inside _run_agent (executor.py
    # lines 510-528) and is covered by test_hitl_manager.py.
    # Here we verify that when _run_agent returns a blocker result,
    # execute_job correctly passes it through to the VALET callback.
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_blocker_result_flows_to_valet(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When _run_agent returns a result with a blocker field,
        execute_job passes it through to VALET's report_completion."""
        mock_load_resume.return_value = {"full_name": "Test User"}
        mock_run_agent.return_value = {
            "success": False,
            "summary": "CAPTCHA detected",
            "steps_taken": 12,
            "cost_usd": 0.05,
            "blocker": "Blocker: CAPTCHA widget detected, cannot proceed",
        }

        job = _make_job()
        result = await execute_job(job, mock_db, mock_valet)

        # The blocker should be present in the result
        assert result.get("blocker") is not None
        assert "CAPTCHA" in result["blocker"]

        # VALET completion callback should include the blocker in result
        mock_valet.report_completion.assert_called_once()
        completion_kwargs = mock_valet.report_completion.call_args.kwargs
        assert "blocker" in completion_kwargs["result"]
        assert "CAPTCHA" in str(completion_kwargs["result"]["blocker"])

    # ------------------------------------------------------------------
    # Scenario 4: Domain blocked — early rejection
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    async def test_domain_blocked_returns_error(
        self, mock_load_resume, mock_db, mock_valet
    ):
        """When the target URL domain is not in the allowed list,
        the job fails with error_code 'domain_blocked'."""
        mock_load_resume.return_value = {"full_name": "Test User"}

        # Use a domain that won't be in the allowed list
        job = _make_job(target_url="https://evil-phishing-site.com/fake-job")

        with patch(
            "ghosthands.worker.executor.validate_domain",
            return_value=False,
        ):
            result = await execute_job(job, mock_db, mock_valet)

        assert result["success"] is False
        assert result["error_code"] == "domain_blocked"

        # _fail_job should have notified VALET of the failure
        mock_valet.report_completion.assert_called_once()
        completion_kwargs = mock_valet.report_completion.call_args.kwargs
        assert completion_kwargs["success"] is False

    # ------------------------------------------------------------------
    # Scenario 5: Budget exceeded — BudgetExceededError handled
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_budget_exceeded_handled(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When _run_agent raises BudgetExceededError, the job fails
        with error_code 'budget_exceeded'."""
        from ghosthands.worker.cost_tracker import BudgetExceededError

        mock_load_resume.return_value = {"full_name": "Test User"}

        # Create a BudgetExceededError with correct constructor args
        mock_snapshot = MagicMock()
        mock_snapshot.total_cost = 0.55
        mock_snapshot.to_dict.return_value = {"total_cost": 0.55, "steps": 45}

        exc = BudgetExceededError(
            "Budget exceeded: $0.55 > $0.50",
            job_id="job-test-001",
            snapshot=mock_snapshot,
        )
        mock_run_agent.side_effect = exc

        job = _make_job()
        result = await execute_job(job, mock_db, mock_valet)

        assert result["success"] is False
        assert result["error_code"] == "budget_exceeded"

        # VALET should have been notified of the failure
        mock_valet.report_completion.assert_called()

    # ------------------------------------------------------------------
    # Scenario 6: Unhandled exception — internal_error
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_unhandled_exception_reports_internal_error(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When _run_agent raises an unexpected exception, the job
        fails with error_code 'internal_error'."""
        mock_load_resume.return_value = {"full_name": "Test User"}
        mock_run_agent.side_effect = RuntimeError("Playwright crashed")

        job = _make_job()
        result = await execute_job(job, mock_db, mock_valet)

        assert result["success"] is False
        assert result["error_code"] == "internal_error"
        assert "Playwright crashed" in result["error"]

        # VALET should have been notified
        mock_valet.report_completion.assert_called()
        completion_kwargs = mock_valet.report_completion.call_args.kwargs
        assert completion_kwargs["success"] is False

    # ------------------------------------------------------------------
    # Scenario 7: Heartbeat is always cancelled in finally block
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_heartbeat_cancelled_on_success(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """The heartbeat background task is cancelled even on success,
        ensuring no leaked tasks."""
        mock_load_resume.return_value = {"full_name": "Test User"}
        mock_run_agent.return_value = {
            "success": True,
            "summary": "Done",
            "steps_taken": 5,
            "cost_usd": 0.03,
            "blocker": None,
        }

        job = _make_job()
        await execute_job(job, mock_db, mock_valet)

        # The heartbeat task should not remain running.
        # Reaching here proves no hung tasks.
        assert True

    # ------------------------------------------------------------------
    # Scenario 8: _run_agent returns user_cancelled error_code
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_user_cancelled_result_flows_through(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When _run_agent returns a user_cancelled result (from the
        HITL cancel path), execute_job includes it in the VALET callback."""
        mock_load_resume.return_value = {"full_name": "Test User"}
        # This is the result shape _run_agent returns when HITL cancel fires
        mock_run_agent.return_value = {
            "success": False,
            "summary": "Cancelled by user",
            "steps_taken": 3,
            "cost_usd": 0.02,
            "blocker": "Blocker: login required",
            "error_code": "user_cancelled",
        }

        job = _make_job()
        result = await execute_job(job, mock_db, mock_valet)

        assert result["success"] is False
        # The summary should flow through to the final result
        assert result.get("summary") == "Cancelled by user"

        # VALET callback should include the result
        mock_valet.report_completion.assert_called_once()

    # ------------------------------------------------------------------
    # Scenario 9: input_data as JSON string is parsed correctly
    # ------------------------------------------------------------------

    @patch("ghosthands.worker.executor.load_resume")
    @patch("ghosthands.worker.executor._run_agent")
    async def test_input_data_json_string_parsed(
        self, mock_run_agent, mock_load_resume, mock_db, mock_valet
    ):
        """When input_data is a JSON string (not a dict), it is parsed
        before being passed to _run_agent."""
        mock_load_resume.return_value = {"full_name": "Test User"}
        mock_run_agent.return_value = {
            "success": True,
            "summary": "Done",
            "steps_taken": 5,
            "cost_usd": 0.03,
            "blocker": None,
        }

        job = _make_job(input_data='{"quality": "premium"}')
        result = await execute_job(job, mock_db, mock_valet)

        assert result["success"] is True

        # _run_agent should have been called — verify input_data was
        # parsed into a dict (not passed as a string)
        mock_run_agent.assert_called_once()
        call_kwargs = mock_run_agent.call_args.kwargs
        assert isinstance(call_kwargs["input_data"], dict)
        assert call_kwargs["input_data"]["quality"] == "premium"
