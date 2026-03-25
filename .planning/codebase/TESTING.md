# Testing Patterns

**Analysis Date:** 2026-03-24

## Test Framework

**Runner:**
- `pytest>=8.3`
- Config: `pyproject.toml` → `[tool.pytest.ini_options]`

**Key pytest plugins:**
- `pytest-asyncio>=0.25` — async test support (`asyncio_mode = "auto"` globally set)
- `pytest-httpserver>=1.1` — local HTTP server for callback/API testing
- `pytest-xdist>=3.6` — parallel test execution

**Assertion Library:**
- Plain `assert` statements — no third-party assertion library

**Run Commands:**
```bash
pytest                         # All tests
pytest tests/unit/             # Unit tests only (no browser/DB)
pytest tests/ci/               # CI integration tests (require browser)
pytest -v                      # Verbose output
pytest -x                      # Stop on first failure
```

## Test File Organization

**Layout:**
```
tests/
├── __init__.py
├── unit/                      # Fast, offline — no browser, no database
│   ├── __init__.py
│   ├── dom/                   # DOM-layer unit tests
│   │   ├── __init__.py
│   │   ├── test_conditional_rescan.py
│   │   ├── test_dropdown_match.py
│   │   ├── test_dropdown_verify.py
│   │   ├── test_fill_executor_platform.py
│   │   ├── test_fill_llm_escalation.py
│   │   └── test_fill_verify.py
│   ├── test_combobox_toggle.py
│   ├── test_cost_summary.py
│   ├── test_desktop_bridge.py     # 2115 lines — most comprehensive
│   ├── test_domhand_fixes.py      # 4807 lines — regression suite
│   ├── test_education_ingestion.py
│   ├── test_local_browser_keep_alive.py
│   ├── test_runtime_learning.py
│   └── test_stagehand_layer.py    # 272 lines
├── ci/                        # Integration + CI tests (require browser)
│   ├── conftest.py            # Shared browser session, mock LLM, HTTP server
│   ├── browser/               # Browser provider and navigation tests
│   ├── infrastructure/        # Registry, config, URL shortening
│   ├── interactions/          # Autocomplete, dropdown, radio integration
│   ├── models/                # LLM provider integration tests
│   ├── security/              # Domain filtering, IP blocking, stealth
│   └── test_*.py              # CI test files (80+ files)
├── integration/               # Full-stack integration tests (bridge, subprocess)
│   ├── test_bridge_profile_real.py
│   ├── test_bridge_protocol_real.py
│   ├── test_bridge_subprocess.py
│   └── test_desktop_e2e_binary.py
├── fixtures/
│   └── domhand_dropdown_control_lab.html   # Static HTML for DOM tests
└── agent_tasks/               # YAML task definitions for agent evaluation
```

**Naming:**
- Test files: `test_{module_or_feature}.py`
- Test functions: `test_{what_it_does}()` — descriptive, reads as a sentence
- Test classes: `Test{Feature}` — groups related tests for a component
- Helper factories: `_make_{thing}(...)` — underscore-prefixed, not collected by pytest

## Test Structure

**Suite Organization — class-grouped:**
```python
class TestEmitBrowserReady:
    """emit_browser_ready(cdp_url) must emit a valid browser_ready JSONL event."""

    def test_emits_correct_type(self):
        ...

    def test_emits_cdp_url_field(self):
        ...

    def test_timestamp_is_present_and_numeric(self):
        ...
```

**Suite Organization — flat functions for small modules:**
```python
def test_max_tokens_scales_with_field_count():
    """max_tokens should scale up for forms with many fields."""
    assert max(4096, min(10 * 128, 16384)) == 4096
    ...

def test_estimate_cost_unknown_model_returns_fallback():
    ...
```

**Module-level setup for stateful singletons:**
```python
def setup_function() -> None:
    reset_runtime_learning_state()
```

**Autouse fixtures for singleton reset:**
```python
@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_stagehand_layer()
    yield
    reset_stagehand_layer()
```

**Patterns:**
- `setup_function()` used for module-level state reset in `test_runtime_learning.py`
- `autouse=True` fixtures for environment isolation and singleton teardown
- `monkeypatch` preferred over `patch` when patching env vars: `monkeypatch.setenv("KEY", "value")`
- `pytest.mark.asyncio` used per-test (also auto-applied via `asyncio_mode = "auto"`)

## Mocking

**Framework:** `unittest.mock` — `AsyncMock`, `MagicMock`, `patch`

**Async mocking pattern:**
```python
from unittest.mock import AsyncMock, MagicMock, patch

mock_client = AsyncMock()
mock_client.sessions = AsyncMock()
mock_client.sessions.start = AsyncMock(return_value=_mock_async_session())

with patch("stagehand.AsyncStagehand", return_value=mock_client):
    result = await layer.ensure_started("ws://localhost:9222")
```

**Monkeypatching module attributes:**
```python
monkeypatch.setattr(
    fill_verify,
    "_read_field_value_for_field",
    AsyncMock(return_value="+1"),
)
```

**Patching with context managers (multi-patch):**
```python
with (
    patch("ghosthands.platforms.get_fill_overrides", return_value={"select": "combobox_toggle"}),
    patch(
        "ghosthands.dom.fill_executor._dispatch_platform_fill_outcome",
        new_callable=AsyncMock,
        return_value=FieldFillOutcome(success=True, matched_label="United States"),
    ) as mock_dispatch,
):
    result = await _fill_single_field(fake_page, field, "United States")
```

**Exception simulation:**
```python
mock_session.act = AsyncMock(side_effect=Exception("timeout"))
```

**What to Mock:**
- External network calls (VALET API, LLM providers, Stagehand SDK)
- Browser/Playwright page objects (`AsyncMock()` as a fake page)
- Singleton layers (`StagehandLayer`, `_jsonl_out` file descriptor)
- Module-level functions in other `ghosthands.*` modules when testing a specific layer

**What NOT to Mock:**
- Pure Python logic (dropdown matching, profile resolution, cost math)
- Pydantic model construction
- `structlog` / logging calls
- Simple dataclass operations

## Fixtures and Factories

**Factory helpers — module-level, underscore-prefixed:**
```python
def _make_field(field_type: str = "select", name: str = "Country") -> FormField:
    return FormField(
        field_id="ff-1",
        field_type=field_type,
        name=name,
        options=[],
    )

def _mock_async_session(session_id="test-session-123", *, success=True):
    """Mimics AsyncSession returned from sessions.start()."""
    s = MagicMock()
    s.id = session_id if success else None
    s.success = success
    s.end = AsyncMock()
    return s
```

**Shared fixtures in `tests/ci/conftest.py`:**
- `browser_session` (`scope="module"`) — real Playwright browser session for CI tests
- `mock_llm` (`scope="function"`) — `BaseChatModel` mock returning a done action
- `cloud_sync` (`scope="function"`) — `CloudSync` pointed at `pytest_httpserver`
- `event_collector` — collects `bubus.BaseEvent` during test execution
- `setup_test_environment` (autouse) — sets env vars, restores on teardown

**`create_mock_llm(actions)` helper in `conftest.py`:**
Returns a `BaseChatModel` `AsyncMock` that replays a sequence of JSON action strings, then falls back to a default `done` action. Used to test agent step sequencing without real LLM calls.

**Location:**
- Unit test helpers: defined inline at top of each test file
- CI shared helpers: `tests/ci/conftest.py`
- Static HTML fixtures: `tests/fixtures/` (e.g., `domhand_dropdown_control_lab.html`)

## Coverage

**Requirements:** Not explicitly enforced (no `--cov` flags in `pyproject.toml`)

**View Coverage:**
```bash
pytest --cov=ghosthands --cov-report=term-missing tests/unit/
```

## Test Types

**Unit Tests (`tests/unit/`):**
- Offline — no browser, no database, no API calls
- Each test file header documents this: `"All tests are offline (no browser, no database, no API calls)."`
- Test pure Python logic: dropdown matching, profile resolution, cost estimation, JSONL event format, runtime learning
- Import internal functions directly (including `_private` functions)

**CI Integration Tests (`tests/ci/`):**
- Require a real browser session (Playwright/Chromium)
- Use `pytest_httpserver` for fake HTTP endpoints
- Cover: browser navigation, DOM serialization, security watchdog, URL filtering, agent loops, CLI flags
- DomHand lab tests use static HTML fixtures: `tests/ci/test_domhand_lab_fixture.py`, `tests/fixtures/domhand_dropdown_control_lab.html`

**Integration Tests (`tests/integration/`):**
- Full-stack: bridge protocol, subprocess communication, desktop binary
- `test_bridge_subprocess.py` tests real process spawning and IPC
- `test_desktop_e2e_binary.py` tests the compiled binary entrypoint

**No E2E tests against real ATS sites** — agent tasks in `tests/agent_tasks/*.yaml` are for manual evaluation, not automated CI.

## Common Patterns

**Async Testing:**
```python
@pytest.mark.asyncio
async def test_layer_start_success():
    layer = StagehandLayer()

    mock_client = AsyncMock()
    mock_client.sessions.start = AsyncMock(return_value=_mock_async_session())

    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        result = await layer.ensure_started("ws://localhost:9222")

    assert result is True
    mock_client.sessions.start.assert_called_once()
```

**Testing exception resilience (graceful degradation):**
```python
@pytest.mark.asyncio
async def test_act_exception_graceful():
    layer = StagehandLayer()
    layer._started = True
    mock_session = AsyncMock()
    mock_session.act = AsyncMock(side_effect=Exception("timeout"))
    layer._session = mock_session

    result = await layer.act("Do something")
    assert result.success is False
    assert "error" in result.message.lower()
```

**Testing JSONL output (capture pattern):**
```python
def _capture_jsonl_output(fn, *args, **kwargs):
    """Call fn with stdout replaced by StringIO; return parsed JSON lines."""
    buf = io.StringIO()
    import ghosthands.output.jsonl as jsonl_mod
    original_guard = jsonl_mod._jsonl_out
    jsonl_mod._jsonl_out = None
    try:
        with patch("sys.stdout", buf):
            fn(*args, **kwargs)
    finally:
        jsonl_mod._jsonl_out = original_guard
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]
```

**Testing Pydantic model construction with `SimpleNamespace` for duck-typing:**
```python
history = SimpleNamespace(
    usage=SimpleNamespace(total_cost=0.25, total_prompt_tokens=100, total_completion_tokens=40),
    history=[SimpleNamespace(result=[SimpleNamespace(metadata={...})])],
)
```

**Testing singleton idempotency:**
```python
async def test_layer_idempotent_start():
    with patch("stagehand.AsyncStagehand", return_value=mock_client):
        await layer.ensure_started("ws://localhost:9222")
        await layer.ensure_started("ws://localhost:9222")
    mock_client.sessions.start.assert_called_once()  # Only one real start
```

---

*Testing analysis: 2026-03-24*
