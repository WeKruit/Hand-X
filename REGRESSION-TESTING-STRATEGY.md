# Hand-X Browser Architecture: Regression Testing Strategy

## Premise

Six work streams are about to modify core Hand-X infrastructure. The concern: "everything is working now, this shouldn't break anything." This document defines the exact tests, commands, gates, and rollback procedures that ensure each stream ships safely.

---

## 1. Pre-Change Baseline ("Green Baseline")

Before any branch is cut, establish the known-good state.

### 1.1 Capture Passing Test Suite

Run the full CI test suite and record results as the baseline.

```bash
cd "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X"

# Full CI test suite (headless browser required)
pytest tests/ci/ -v --tb=short 2>&1 | tee baseline-test-results.txt

# Record test count and pass/fail summary
pytest tests/ci/ --co -q 2>&1 | tail -5 > baseline-test-count.txt
```

**Must-pass categories before any work begins:**

| Category | Directory | What it validates |
|----------|-----------|-------------------|
| Browser lifecycle | `tests/ci/browser/test_session_start.py` | Session start, reuse, event system |
| Browser navigation | `tests/ci/browser/test_navigation.py` | Page navigation, URL handling |
| Browser tabs | `tests/ci/browser/test_tabs.py` | Tab create, switch, close |
| DOM serializer | `tests/ci/browser/test_dom_serializer.py` | DOM snapshot extraction |
| Screenshots | `tests/ci/browser/test_screenshot.py` | Screenshot capture |
| DomHand actions | `tests/ci/test_domhand_click_button.py`, `test_domhand_upload.py` | Form interactions |
| Security/domain | `tests/ci/security/test_domain_filtering.py` | URL allowlist enforcement |
| Security/IP | `tests/ci/security/test_ip_blocking.py` | IP-based blocking |
| Security/flags | `tests/ci/security/test_security_flags.py` | Security flag enforcement |
| Security/sensitive | `tests/ci/security/test_sensitive_data.py` | Credential masking |
| Infrastructure | `tests/ci/infrastructure/test_config.py` | Settings loading |
| Infrastructure | `tests/ci/infrastructure/test_registry_core.py` | Action registry |
| CLI | `tests/ci/test_cli_headed_flag.py`, `test_cli_install_init.py` | CLI argument parsing |
| Agent | `tests/ci/test_tools.py` | Tool registration |
| Interactions | `tests/ci/interactions/test_dropdown_*.py`, `test_radio_buttons.py`, `test_autocomplete_interaction.py` | Form widget interactions |

### 1.2 Capture Current JSONL Output Schema

Record the current JSONL output as a contract snapshot. This is the "before" for S2.

```bash
# Run Hand-X in human mode with a simple test to capture current JSONL shape
python -c "
from ghosthands.output.jsonl import emit_event
import json, io, sys

# Capture what emit_event currently produces
original_stdout = sys.stdout
buf = io.StringIO()
sys.stdout = buf
emit_event('status', message='test')
emit_event('field_filled', field='name', value='Jane', method='domhand')
emit_event('done', success=True, message='done')
sys.stdout = original_stdout

for line in buf.getvalue().strip().split('\n'):
    obj = json.loads(line)
    print(json.dumps(obj, indent=2))
" 2>&1 | tee baseline-jsonl-schema.txt
```

This captures the current key names (`type` vs `event`) and field shapes as a contract reference.

### 1.3 Anti-Detection Score Baseline

Record current bot detection scores before S1 stealth layer work begins.

```bash
# Manual: run headed browser against detection sites and screenshot results
# Store screenshots in tests/baselines/

# Sites to test:
# 1. https://bot.sannysoft.com -- comprehensive bot detection
# 2. https://arh.antoinevastel.com/bots/areyouheadless -- headless detection
# 3. https://abrahamjuliot.github.io/creepjs/ -- fingerprint analysis

# Automated baseline capture (run manually, needs display):
python -c "
import asyncio
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

async def capture_baseline():
    profile = BrowserProfile(headless=False, keep_alive=False)
    session = BrowserSession(browser_profile=profile)
    await session.start()
    page = await session.get_current_page()

    results = {}
    results['webdriver'] = await page.evaluate('navigator.webdriver')
    results['plugins_count'] = await page.evaluate('navigator.plugins.length')
    results['chrome_runtime'] = await page.evaluate('typeof window.chrome?.runtime')
    results['languages'] = await page.evaluate('JSON.stringify(navigator.languages)')

    import json
    print(json.dumps(results, indent=2))
    await session.kill()

asyncio.run(capture_baseline())
" 2>&1 | tee baseline-antidetection-scores.txt
```

### 1.4 Linting Baseline

```bash
ruff check . 2>&1 | tee baseline-lint.txt
ruff format --check . 2>&1 | tee baseline-format.txt
```

---

## 2. Per-Stream Regression Gates

Each stream must pass ALL three gate levels before merge.

### Gate Structure (applied to every stream)

```
Gate 1: EXISTING TESTS PASS (no regressions)
  pytest tests/ci/ -v --tb=short
  ruff check . && ruff format --check .

Gate 2: NEW TESTS PASS (feature works)
  pytest tests/ci/<stream-specific-tests> -v

Gate 3: INTEGRATION VALIDATION (end-to-end check)
  Stream-specific integration test (defined below)
```

---

### Stream 1: JS Injection Stealth Layer

**Files touched:** `browser_use/browser/stealth/` (new), `browser_use/browser/profile.py`, `browser_use/browser/session.py`

#### Gate 1: No Regressions

```bash
# Full browser test suite -- stealth must not break browser lifecycle
pytest tests/ci/browser/ -v --tb=short

# Full security suite -- stealth must not weaken security
pytest tests/ci/security/ -v --tb=short

# Full interaction suite -- stealth JS must not interfere with DOM actions
pytest tests/ci/interactions/ -v --tb=short
pytest tests/ci/test_domhand_click_button.py tests/ci/test_domhand_upload.py -v

# DomHand must still work with stealth scripts present
pytest tests/ci/test_structured_extraction.py -v
```

#### Gate 2: New Tests Pass

Tests to write in `tests/ci/security/test_stealth_injection.py`:

```
test_webdriver_not_detectable
  - Launch with StealthConfig(enabled=True)
  - Assert navigator.webdriver is undefined/false

test_chrome_runtime_exists
  - Assert window.chrome.runtime.sendMessage is a function

test_plugins_populated
  - Assert navigator.plugins.length >= 3

test_stealth_survives_navigation
  - Navigate to page A, check webdriver
  - Navigate to page B, check webdriver again
  - Both must be undefined

test_stealth_survives_iframe
  - Create iframe, check navigator.webdriver inside it

test_stealth_disabled_skips_injection
  - Launch with StealthConfig(enabled=False)
  - Assert navigator.webdriver is NOT patched (still true/present)

test_individual_patch_disable
  - StealthConfig(webdriver_patch=False) -- webdriver still detectable
  - Other patches still applied

test_stealth_does_not_break_page_evaluate
  - After stealth injection, page.evaluate() still works for DomHand
```

#### Gate 3: Integration Validation

```bash
# Manual: launch with stealth, visit bot.sannysoft.com, compare to baseline
# Automated:
python -c "
import asyncio
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
# from browser_use.browser.stealth.config import StealthConfig  # new

async def validate():
    profile = BrowserProfile(headless=True)
    # profile.stealth = StealthConfig(enabled=True)  # new
    session = BrowserSession(browser_profile=profile)
    await session.start()
    page = await session.get_current_page()

    wd = await page.evaluate('navigator.webdriver')
    assert wd is None or wd is False, f'webdriver leaked: {wd}'

    plugins = await page.evaluate('navigator.plugins.length')
    assert plugins >= 3, f'plugins count too low: {plugins}'

    cr = await page.evaluate('typeof window.chrome?.runtime?.sendMessage')
    assert cr == 'function', f'chrome.runtime missing: {cr}'

    print('STEALTH INTEGRATION: PASS')
    await session.kill()

asyncio.run(validate())
"
```

#### Rollback Criteria

- If any `tests/ci/browser/` test regresses: revert S1 entirely
- If stealth causes CAPTCHA walls on known ATS sites: set `StealthConfig(enabled=False)` as default
- Feature flag: `GH_STEALTH_ENABLED` env var (default `false` initially, flip to `true` after validation)

---

### Stream 2: JSONL Schema Normalization + Lease Protocol

**Files touched:** `ghosthands/output/jsonl.py`, `ghosthands/cli.py`, `GH-Desktop-App/src/main/runHandX.ts`

#### Gate 1: No Regressions

```bash
# All existing tests (JSONL is used internally in CLI tests)
pytest tests/ci/ -v --tb=short

# CLI-specific tests
pytest tests/ci/test_cli_headed_flag.py tests/ci/test_cli_install_init.py -v
```

#### Gate 2: New Tests Pass

Tests to write in `tests/unit/test_jsonl_schema.py`:

```
test_emit_event_uses_event_key_not_type
  - Capture emit_event() output to StringIO
  - Parse JSON, assert "event" key exists, "type" key does NOT exist

test_all_event_types_use_event_key
  - Call emit_status, emit_field_filled, emit_field_failed, emit_progress, emit_done, emit_error, emit_cost
  - Assert every line has "event" key

test_emit_handshake_event
  - emit_handshake() produces {"event": "handshake", "protocol_version": 2, ...}

test_emit_browser_ready_event
  - emit_browser_ready("http://...") produces {"event": "browser_ready", "cdpUrl": "http://..."}

test_emit_lease_acquired_event
  - emit_lease_acquired("lease-1", "job-1") produces correct shape

test_emit_lease_released_event
  - emit_lease_released("lease-1", "completed") produces correct shape

test_emit_lease_heartbeat_event
  - emit_lease_heartbeat("lease-1") produces correct shape

test_timestamp_present_on_all_events
  - Every emitted event has "timestamp" as integer (ms)

test_none_values_omitted
  - emit_status("msg", job_id="") should NOT have jobId in output
```

Tests to write in `tests/unit/test_jsonl_field_mismatch.py`:

```
test_field_failed_uses_reason_key
  - PLAN: Desktop expects "reason", Hand-X emits "error"
  - After S2 fix: emit_field_failed emits "reason" key

test_progress_uses_step_maxsteps
  - PLAN: Desktop expects "step"/"maxSteps", Hand-X emits "filled"/"total"
  - After S2 fix: emit_progress maps correctly
```

#### Gate 3: Integration Validation -- Cross-Repo Compatibility

This is the most critical integration test because S2 modifies the IPC contract between Hand-X and Desktop.

```bash
# Step 1: Spawn Hand-X as subprocess, capture JSONL stdout
python -c "
import subprocess, json, sys

# Run Hand-X with minimal args to get JSONL output
proc = subprocess.Popen(
    [sys.executable, '-m', 'ghosthands',
     '--job-url', 'https://example.com',
     '--profile', '{\"name\": \"Test\"}',
     '--output-format', 'jsonl',
     '--max-steps', '1'],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

# Read first few lines
lines = []
for i, line in enumerate(proc.stdout):
    if i > 5:
        proc.kill()
        break
    line = line.strip()
    if line:
        lines.append(json.loads(line))

# Validate contract
for obj in lines:
    assert 'event' in obj, f'Missing event key: {obj}'
    assert 'type' not in obj or obj.get('type') != obj.get('event'), f'Old type key present: {obj}'
    assert 'timestamp' in obj, f'Missing timestamp: {obj}'

print(f'Validated {len(lines)} events, all have event key')
print(json.dumps(lines, indent=2))
proc.kill()
"
```

```typescript
// Step 2: Desktop-side validation (run in GH-Desktop-App test harness)
// tests/__tests__/handx-jsonl-compat.test.ts

describe('Hand-X JSONL v2 compatibility', () => {
  const sampleEvents = [
    '{"event":"handshake","protocol_version":2,"timestamp":1234567890}',
    '{"event":"status","message":"Starting","timestamp":1234567891}',
    '{"event":"field_filled","field":"name","value":"Jane","method":"domhand","timestamp":1234567892}',
    '{"event":"field_failed","field":"phone","reason":"not found","timestamp":1234567893}',
    '{"event":"progress","step":3,"maxSteps":10,"description":"Filling form","timestamp":1234567894}',
    '{"event":"cost","total_usd":0.01,"prompt_tokens":100,"completion_tokens":50,"timestamp":1234567895}',
    '{"event":"done","success":true,"message":"Complete","fields_filled":5,"timestamp":1234567896}',
    '{"event":"lease_acquired","leaseId":"l-1","jobId":"j-1","timestamp":1234567897}',
    '{"event":"lease_released","leaseId":"l-1","reason":"completed","timestamp":1234567898}',
    '{"event":"lease_heartbeat","leaseId":"l-1","timestamp":1234567899}',
    '{"event":"browser_ready","cdpUrl":"http://127.0.0.1:9242/","timestamp":1234567900}',
  ];

  for (const line of sampleEvents) {
    it(`parses ${JSON.parse(line).event} event`, () => {
      const parsed = JSON.parse(line);
      expect(typeof parsed.event).toBe('string');
      expect(parsed.event).not.toBe('');
      expect(typeof parsed.timestamp).toBe('number');
    });
  }

  it('rejects lines without event key (old format)', () => {
    const oldFormat = '{"type":"status","message":"test","timestamp":123}';
    const parsed = JSON.parse(oldFormat);
    // Desktop parser check from runHandX.ts line 1291
    expect(typeof (parsed as { event?: unknown }).event).not.toBe('string');
  });
});
```

**Backward-compat shim (deploy safety net):**

During transition, Desktop should accept both `event` and `type` keys:

```typescript
// In runHandX.ts event parsing, temporarily:
const eventKey = (parsed as any).event ?? (parsed as any).type;
if (typeof eventKey !== 'string') { /* malformed, skip */ }
```

Ship Desktop with shim first. Once Hand-X v2 schema is confirmed stable, remove the shim.

#### Rollback Criteria

- If Desktop drops events: git revert the `jsonl.py` change, keep shim in Desktop
- S2 is the only stream that requires coordinated cross-repo deployment
- Deploy order: Desktop shim first (accepts both), then Hand-X `type`->`event` change

---

### Stream 3: keep_alive + HITL Fix

**Files touched:** `ghosthands/agent/factory.py`, `ghosthands/worker/executor.py`, `ghosthands/agent/hooks.py`

#### Gate 1: No Regressions

```bash
# Browser lifecycle tests -- critical for keep_alive
pytest tests/ci/browser/test_session_start.py -v

# Agent tests
pytest tests/ci/test_tools.py tests/ci/test_ai_step.py -v

# All CI tests
pytest tests/ci/ -v --tb=short
```

#### Gate 2: New Tests Pass

Tests to write in `tests/unit/test_keep_alive_fix.py`:

```
test_run_job_agent_keep_alive_true_preserves_browser
  - Create agent with BrowserProfile(keep_alive=True)
  - Mock the agent.run() to return quickly
  - After run_job_agent() returns, assert browser session is NOT killed
  - Assert browser PID still exists (psutil.pid_exists)

test_run_job_agent_keep_alive_false_kills_browser
  - Create agent with BrowserProfile(keep_alive=False)
  - After run_job_agent() returns, assert browser is killed

test_run_job_agent_default_keep_alive_preserves_browser
  - factory.py line 131 sets keep_alive=True by default
  - Verify this default path does NOT kill browser
```

Tests to write in `tests/unit/test_hitl_wiring.py`:

```
test_executor_calls_hitl_pause_on_blocker
  - Mock Database, ValetClient, HITLManager
  - Provide job result with blocker="Blocker: CAPTCHA wall"
  - Assert hitl.pause_job() was called with correct args

test_executor_waits_for_hitl_resume
  - Mock hitl.wait_for_resume() to return {"action": "resume"}
  - Verify executor continues after resume

test_executor_cancels_on_hitl_cancel
  - Mock hitl.wait_for_resume() to return {"action": "cancel"}
  - Verify executor returns success=False, error_code="user_cancelled"

test_executor_handles_hitl_timeout
  - Mock hitl.wait_for_resume() to return None (timeout)
  - Verify executor handles gracefully (does not crash)
```

#### Gate 3: Integration Validation

```bash
# Manual test: CLI path with keep_alive=True
python -m ghosthands \
  --job-url "https://example.com" \
  --test-data examples/apply_to_job_sample_data.json \
  --output-format human \
  --max-steps 3
# Verify: browser window stays open after agent completes
# Verify: Ctrl+C closes browser cleanly
```

#### Rollback Criteria

- If browser processes leak (orphaned chrome instances): add `GH_FORCE_KILL_ON_COMPLETE=true` env var
- If HITL deadlocks executor: remove HITL wiring, keep the keep_alive fix
- S3 changes are fully internal to Hand-X -- no cross-repo coordination needed

---

### Stream 4: Domain Lockdown CLI

**Files touched:** `ghosthands/cli.py`, `ghosthands/security/domain_lockdown.py`

#### Gate 1: No Regressions

```bash
# Security tests -- domain lockdown must not break existing filtering
pytest tests/ci/security/ -v

# CLI tests
pytest tests/ci/test_cli_headed_flag.py tests/ci/test_cli_install_init.py -v

# All CI
pytest tests/ci/ -v --tb=short
```

#### Gate 2: New Tests Pass

Tests to write in `tests/unit/test_domain_lockdown_cli.py`:

```
test_cli_adds_allowed_domains_arg
  - Parse args with --allowed-domains "example.com,other.com"
  - Assert args.allowed_domains == ["example.com", "other.com"]

test_cli_creates_domain_lockdown_from_job_url
  - With --job-url "https://company.myworkdayjobs.com/..."
  - Assert DomainLockdown includes myworkdayjobs.com and Workday subdomains

test_cli_passes_lockdown_to_browser_profile
  - Verify BrowserProfile receives allowed_domains from lockdown

test_domain_lockdown_workday_domains
  - DomainLockdown(job_url="https://company.myworkdayjobs.com/...", platform="workday")
  - is_allowed("https://wd5.myworkday.com/...") returns True
  - is_allowed("https://evil.com") returns False

test_domain_lockdown_additional_domains
  - DomainLockdown(..., additional_domains=["sso.company.com"])
  - is_allowed("https://sso.company.com/login") returns True
```

#### Gate 3: Integration Validation

```bash
# Test that CLI actually blocks navigation (dry run)
python -m ghosthands \
  --job-url "https://boards.greenhouse.io/test" \
  --profile '{"name":"Test"}' \
  --output-format human \
  --allowed-domains "greenhouse.io" \
  --max-steps 2
# Verify: agent only navigates within greenhouse.io domain
```

#### Rollback Criteria

- S4 is additive only -- if it breaks, git revert removes the `--allowed-domains` flag
- Existing behavior (no lockdown in CLI) is restored on revert

---

### Stream 5: BrowserProvider Abstraction

**Files touched:** `browser_use/browser/providers/` (new), `browser_use/browser/watchdogs/local_browser_watchdog.py`, `browser_use/browser/profile.py`, `browser_use/browser/session.py`

#### Gate 1: No Regressions -- CRITICAL

S5 refactors the browser launch path. Every browser test must pass.

```bash
# ALL browser tests -- this is the highest-risk gate
pytest tests/ci/browser/ -v --tb=short

# All interaction tests (verify browser still works for real actions)
pytest tests/ci/interactions/ -v --tb=short

# DomHand tests (verify DOM manipulation still works)
pytest tests/ci/test_domhand_click_button.py tests/ci/test_domhand_upload.py -v

# Full security suite (verify security watchdog still works)
pytest tests/ci/security/ -v --tb=short

# Full CI suite
pytest tests/ci/ -v --tb=short
```

#### Gate 2: New Tests Pass

Tests to write in `tests/ci/browser/test_browser_provider.py`:

```
test_chromium_provider_launches_browser
  - ChromiumProvider().launch(profile) returns (cdp_url, pid)
  - cdp_url starts with "http://127.0.0.1:"
  - pid is a valid process

test_chromium_provider_kills_browser
  - Launch, then kill, assert process no longer exists

test_chromium_provider_get_default_args
  - get_default_args(profile) returns list containing expected Chrome flags

test_provider_registry_chromium
  - ProviderRegistry.get('chromium') returns ChromiumProvider

test_provider_registry_firefox_not_implemented
  - ProviderRegistry.get('firefox') raises NotImplementedError

test_browser_profile_engine_field
  - BrowserProfile(engine='chromium') is valid
  - BrowserProfile(engine='auto') is valid

test_engine_chromium_identical_behavior
  - BrowserProfile(engine='chromium') produces identical launch behavior
  - Compare args, CDP URL format, page.evaluate results
```

#### Gate 3: Integration Validation

```bash
# Run the full agent with the new provider path
python -m ghosthands \
  --job-url "https://example.com" \
  --profile '{"name":"Test"}' \
  --output-format human \
  --max-steps 2
# Verify: browser launches correctly, agent takes actions, browser closes

# Verify provider is used correctly (check logs)
python -c "
import asyncio
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

async def test():
    profile = BrowserProfile(headless=True, engine='chromium')
    session = BrowserSession(browser_profile=profile)
    await session.start()

    page = await session.get_current_page()
    title = await page.title()
    print(f'Page title: {title}')
    print('PROVIDER INTEGRATION: PASS')

    await session.kill()

asyncio.run(test())
"
```

#### Rollback Criteria

- If ANY `tests/ci/browser/` test regresses: revert S5 entirely
- Composition fallback: keep `LocalBrowserWatchdog` unchanged, have `ChromiumProvider` wrap it rather than replace it
- S5 is the highest-risk refactor -- extra review required before merge

---

### Stream 6: Camoufox Engine

**Files touched:** `browser_use/browser/providers/camoufox.py` (new), `browser_use/browser/providers/route_selector.py` (new), `ghosthands/agent/factory.py`, `ghosthands/cli.py`

#### Gate 1: No Regressions

```bash
# All tests must pass with default engine='chromium'
pytest tests/ci/ -v --tb=short
```

#### Gate 2: New Tests Pass

Tests to write in `tests/ci/browser/test_camoufox_provider.py`:

```
test_camoufox_provider_launch (skip if camoufox not installed)
  - CamoufoxProvider().launch(profile) returns (cdp_url, pid)

test_camoufox_page_evaluate (skip if camoufox not installed)
  - page.evaluate("1 + 1") returns 2

test_camoufox_navigation (skip if camoufox not installed)
  - Navigate to about:blank, verify URL
```

Tests to write in `tests/ci/browser/test_route_selector.py`:

```
test_workday_routes_to_firefox
  - RouteSelector().select_engine("https://company.myworkdayjobs.com/...") == "firefox"

test_greenhouse_routes_to_chromium
  - RouteSelector().select_engine("https://boards.greenhouse.io/...") == "chromium"

test_unknown_site_routes_to_chromium
  - RouteSelector().select_engine("https://example.com") == "chromium"

test_engine_auto_uses_route_selector
  - BrowserProfile(engine='auto') with Workday URL -> firefox engine
```

#### Gate 3: Integration Validation

```bash
# Camoufox available: full agent run
python -m ghosthands \
  --job-url "https://example.com" \
  --profile '{"name":"Test"}' \
  --engine firefox \
  --max-steps 2

# Camoufox unavailable: verify graceful fallback
python -m ghosthands \
  --job-url "https://example.com" \
  --profile '{"name":"Test"}' \
  --engine auto \
  --max-steps 2
# Should fall back to Chromium with a warning log
```

#### Rollback Criteria

- Feature flag: `GH_ENABLE_FIREFOX=true` env var (default `false`)
- If Camoufox CDP is incompatible: disable Firefox engine, keep provider abstraction
- S6 is behind S5's abstraction -- reverts are independent

---

## 3. Critical Path Testing

### 3.1 Happy Path: Worker Flow (EC2)

This is the primary production flow that must NEVER break.

```
Worker polls job -> executor.execute_job() -> factory.run_job_agent()
  -> Agent.run() -> DomHand fills form -> done action -> result reported to VALET
```

**Test: `tests/integration/test_worker_happy_path.py`**

```python
"""Integration test for the full worker happy path.

Uses a local HTML form instead of a live ATS site.
Mocks Database and ValetClient to avoid external dependencies.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghosthands.worker.executor import execute_job


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.heartbeat = AsyncMock()
    db.write_job_result = AsyncMock()
    db.write_job_event = AsyncMock()
    db.update_job_status = AsyncMock()
    db.load_credentials = AsyncMock(return_value=None)
    db._require_pool = MagicMock()
    return db


@pytest.fixture
def mock_valet():
    valet = AsyncMock()
    valet.report_running = AsyncMock()
    valet.report_progress = AsyncMock()
    valet.report_completion = AsyncMock()
    valet.report_needs_human = AsyncMock()
    return valet


@pytest.mark.asyncio
async def test_executor_happy_path(mock_db, mock_valet):
    """Executor receives job -> runs agent -> reports success."""
    job = {
        "id": "00000000-0000-0000-0000-000000000001",
        "user_id": "user-1",
        "target_url": "https://boards.greenhouse.io/test",
        "job_type": "apply",
        "input_data": {},
        "valet_task_id": "task-1",
    }

    # Mock resume loading
    with patch("ghosthands.worker.executor.load_resume", new_callable=AsyncMock) as mock_resume:
        mock_resume.return_value = {"full_name": "Jane Doe", "email": "jane@example.com"}

        # Mock agent run to simulate success
        with patch("ghosthands.worker.executor.run_job_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = {
                "success": True,
                "steps": 5,
                "cost_usd": 0.02,
                "extracted_text": "Application submitted",
                "blocker": None,
            }

            result = await execute_job(job, mock_db, mock_valet)

    assert result["success"] is True
    mock_valet.report_running.assert_called_once()
    mock_valet.report_completion.assert_called_once()
    mock_db.write_job_result.assert_called_once()
```

### 3.2 Happy Path: Desktop Flow

```
Desktop dispatches -> Hand-X subprocess -> JSONL stdout
  -> Agent fills form -> done event -> browser stays open -> review command -> close
```

**Test: `tests/integration/test_desktop_happy_path.py`**

```python
"""Integration test for the Desktop subprocess flow.

Spawns Hand-X as a subprocess and validates JSONL output.
Does NOT require Desktop app -- tests the Hand-X side of the contract.
"""

import asyncio
import json
import subprocess
import sys

import pytest


@pytest.mark.asyncio
async def test_cli_jsonl_output_contract():
    """Spawn Hand-X, verify JSONL output matches Desktop expectations."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ghosthands",
        "--job-url", "https://example.com",
        "--profile", '{"name": "Test User", "email": "test@example.com"}',
        "--output-format", "jsonl",
        "--max-steps", "1",
        "--headless",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    events = []
    try:
        # Read JSONL lines with timeout
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            if not line:
                break
            decoded = line.decode().strip()
            if decoded:
                obj = json.loads(decoded)
                events.append(obj)
                # Stop after done or error event
                if obj.get("event") in ("done", "error"):
                    break
    except asyncio.TimeoutError:
        pass
    finally:
        proc.kill()
        await proc.wait()

    # Validate contract
    assert len(events) > 0, "No JSONL events received"

    for evt in events:
        # Every event must have "event" key (not "type")
        assert "event" in evt, f"Missing 'event' key in: {evt}"
        assert isinstance(evt["timestamp"], int), f"Bad timestamp in: {evt}"

    # First event should be handshake (after S2)
    # assert events[0]["event"] == "handshake"

    event_types = [e["event"] for e in events]
    print(f"Events received: {event_types}")
```

### 3.3 Testing Without a Live ATS

Use a local HTTP server serving HTML forms that mimic ATS behavior.

```python
"""Fixture: local ATS-like form server for integration tests."""

from pytest_httpserver import HTTPServer


SIMPLE_FORM_HTML = """
<!DOCTYPE html>
<html>
<body>
  <h1>Job Application</h1>
  <form id="application-form">
    <label for="name">Full Name</label>
    <input type="text" id="name" name="name" required>

    <label for="email">Email</label>
    <input type="email" id="email" name="email" required>

    <label for="phone">Phone</label>
    <input type="tel" id="phone" name="phone">

    <select id="experience" name="experience">
      <option value="">Select experience</option>
      <option value="0-2">0-2 years</option>
      <option value="3-5">3-5 years</option>
      <option value="5+">5+ years</option>
    </select>

    <button type="submit">Submit Application</button>
  </form>
</body>
</html>
"""


@pytest.fixture
def local_ats(httpserver: HTTPServer):
    """Serve a local ATS-like form for integration tests."""
    httpserver.expect_request("/apply").respond_with_data(
        SIMPLE_FORM_HTML, content_type="text/html"
    )
    return httpserver.url_for("/apply")
```

---

## 4. Rollback Plan Per Stream

### 4.1 Rollback Decision Matrix

| Stream | Revert independently? | Feature flag available? | Cross-repo impact? |
|--------|----------------------|------------------------|-------------------|
| S1: Stealth | Yes | `GH_STEALTH_ENABLED` | None |
| S2: JSONL | Partial (needs Desktop shim) | Protocol version check | Yes -- Desktop must tolerate old format |
| S3: keep_alive | Yes | `GH_FORCE_KILL_ON_COMPLETE` | None |
| S4: Domain CLI | Yes | None needed (additive) | None |
| S5: Provider | Yes (but must revert S6 first) | None | None |
| S6: Camoufox | Yes | `GH_ENABLE_FIREFOX` | None |

### 4.2 Rollback Procedure

```bash
# Standard revert (any stream)
git revert <merge-commit-hash>
git push origin main

# S2 special case: revert Hand-X but keep Desktop shim
# Desktop shim accepts both "event" and "type" keys, so it's safe
cd "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X"
git revert <s2-merge-hash>
# Desktop continues to work with old "type" format

# S5 + S6 cascade: must revert S6 before S5
git revert <s6-merge-hash>
git revert <s5-merge-hash>
```

### 4.3 Feature Flags vs Git Revert

**Use feature flags for:**
- S1 (stealth): `GH_STEALTH_ENABLED=false` disables all injection without code revert
- S3 (keep_alive): `GH_FORCE_KILL_ON_COMPLETE=true` restores old kill behavior
- S6 (Camoufox): `GH_ENABLE_FIREFOX=false` disables Firefox engine

**Use git revert for:**
- S2 (JSONL): Schema change is binary -- either emit `event` or `type`
- S4 (domain CLI): Simple additive change, clean revert
- S5 (provider): Structural refactor, revert is cleaner than flag

---

## 5. Monitoring After Deploy

### 5.1 Key Metrics to Watch

| Metric | Source | Normal | Alert Threshold |
|--------|--------|--------|-----------------|
| Job success rate | VALET `gh_automation_jobs.status` | >70% | <50% for 1 hour |
| Agent step count per job | Cost tracker `step_count` | 15-40 steps | >80 steps (agent stuck) |
| LLM cost per job | Cost tracker `total_cost_usd` | $0.02-0.15 | >$0.50 (budget exceeded) |
| Browser launch success | Worker logs `executor.launching_agent` | 100% | Any failure |
| JSONL event delivery | Desktop `malformedStdoutLines` count | 0 | >0 (schema mismatch) |
| Browser process leaks | EC2 `chrome` process count | 0-1 per worker | >3 (leak) |
| CAPTCHA/block rate | Agent output `blocker:` keyword | <10% | >30% (stealth regression) |
| DomHand fill success | `field_filled` vs `field_failed` ratio | >80% | <60% |

### 5.2 Monitoring Commands

```bash
# Check for orphaned browser processes on EC2
ssh -i ~/.ssh/wekruit-atm-server.pem ubuntu@<worker-ip> \
  "ps aux | grep -c 'chrome\|chromium\|camoufox' | head -1"

# Check JSONL event delivery (Desktop logs)
# In Desktop dev tools console:
# Look for "[runHandX] Ignoring non-event JSONL" warnings
# If count > 0 after S2 deploy, JSONL schema mismatch is occurring

# Check job success rate (Postgres)
psql $DATABASE_URL -c "
  SELECT
    status,
    COUNT(*) as count,
    ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER () * 100, 1) as pct
  FROM gh_automation_jobs
  WHERE created_at > NOW() - INTERVAL '1 hour'
  GROUP BY status
  ORDER BY count DESC;
"
```

### 5.3 Catching Regressions That Tests Miss

Tests cannot catch everything. These manual checks cover the gaps:

1. **Anti-detection regression:** After S1 deploy, run 3 real Workday applications. Compare block rate to pre-S1 baseline. If block rate increases, disable stealth.

2. **Browser process leak:** After S3 deploy, monitor EC2 worker memory for 24 hours. If `chrome` process count exceeds 3 per worker, `keep_alive` fix is leaking.

3. **HITL deadlock:** After S3 deploy, trigger a manual blocker scenario. Verify the agent pauses and VALET shows "needs human" status within 30 seconds.

4. **Desktop event delivery:** After S2 deploy, monitor Desktop's console for "Ignoring malformed/non-event JSONL" warnings for 24 hours. Any occurrence means the schema change broke the contract.

5. **Form fill accuracy:** After any stream, run 5 real applications across 3 platforms (Workday, Greenhouse, Lever). Compare fill accuracy to pre-change baseline.

---

## 6. Coordinated Testing for S2 (Cross-Repo)

### 6.1 The Problem

S2 modifies the JSONL contract between Hand-X (`ghosthands/output/jsonl.py`) and Desktop (`GH-Desktop-App/src/main/runHandX.ts`). Both repos must change atomically from the user's perspective.

### 6.2 Testing Without Shipping

```
Phase 1: Test locally (no deploy)
  1. Branch Hand-X: feat/handx-lease-protocol
  2. Branch Desktop: feat/desktop-lease-protocol
  3. Build Desktop from branch (npm run build in GH-Desktop-App)
  4. Point Desktop at Hand-X branch binary (resolveHandXBinaryPath override)
  5. Run a local job dispatch -- verify JSONL flows correctly
  6. Check Desktop's malformedStdoutLines count == 0

Phase 2: Ship with backward compat
  1. Merge Desktop shim FIRST (accepts both "event" and "type")
  2. Ship Desktop update to users
  3. Merge Hand-X JSONL change ("type" -> "event")
  4. Desktop continues to work with both old and new Hand-X binaries

Phase 3: Clean up
  1. After confirming all users are on new Hand-X binary (48 hours)
  2. Remove backward-compat shim from Desktop
  3. Ship Desktop cleanup update
```

### 6.3 Local Cross-Repo Test Harness

```bash
# Terminal 1: Build Hand-X from S2 branch
cd "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X"
git checkout feat/handx-lease-protocol
uv pip install -e ".[dev]"

# Terminal 2: Build Desktop from S2 branch
cd "/Users/adam/Desktop/WeKruit/VALET & GH/GH-Desktop-App"
git checkout feat/desktop-lease-protocol
npm install && npm run build

# Terminal 3: Integration test
cd "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X"

# Spawn Hand-X and pipe to a validator
python -m ghosthands \
  --job-url "https://boards.greenhouse.io/test" \
  --profile '{"name":"Test","email":"test@test.com"}' \
  --output-format jsonl \
  --max-steps 2 \
  --headless \
  2>/dev/null | python -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    obj = json.loads(line)
    event_key = obj.get('event')
    type_key = obj.get('type')
    if event_key:
        print(f'OK: event={event_key}')
    elif type_key:
        print(f'FAIL: still using type key: type={type_key}')
        sys.exit(1)
    else:
        print(f'FAIL: no event or type key: {obj}')
        sys.exit(1)
print('ALL EVENTS VALID')
"
```

### 6.4 JSONL Compatibility Matrix

| Hand-X version | Desktop version | Works? | Notes |
|----------------|-----------------|--------|-------|
| Old (`type` key) | Old (expects `event`) | NO | Current bug -- all events dropped |
| Old (`type` key) | New (accepts both) | YES | Shim handles it |
| New (`event` key) | Old (expects `event`) | YES | Fixed |
| New (`event` key) | New (accepts both) | YES | Ideal state |

The backward-compat shim makes all combinations work except Old+Old (which is already broken today).

---

## 7. Test Execution Order (Merge Sequence)

| Order | Stream | Pre-Merge Command | Post-Merge Validation |
|-------|--------|-------------------|-----------------------|
| 1st | S2: JSONL | `pytest tests/ci/ -v && pytest tests/unit/test_jsonl_schema.py -v` | Run JSONL validator against subprocess output |
| 2nd | S3: Lifecycle | `pytest tests/ci/ -v && pytest tests/unit/test_keep_alive_fix.py -v && pytest tests/unit/test_hitl_wiring.py -v` | Manual: verify browser stays open after completion |
| 3rd | S1: Stealth | `pytest tests/ci/ -v && pytest tests/ci/security/test_stealth_injection.py -v` | Run against bot.sannysoft.com, compare to baseline |
| 4th | S4: Domain CLI | `pytest tests/ci/ -v && pytest tests/unit/test_domain_lockdown_cli.py -v` | Manual: verify domain blocking in CLI |
| 5th | S5: Provider | `pytest tests/ci/ -v && pytest tests/ci/browser/test_browser_provider.py -v` | Full agent run with new provider path |
| 6th | S6: Camoufox | `pytest tests/ci/ -v && pytest tests/ci/browser/test_camoufox_provider.py -v && pytest tests/ci/browser/test_route_selector.py -v` | Firefox agent run (skip if not installed) |

**After ALL streams merge:**

```bash
# Final comprehensive validation
pytest tests/ci/ -v --tb=short 2>&1 | tee post-merge-test-results.txt

# Compare to baseline
diff baseline-test-count.txt <(pytest tests/ci/ --co -q 2>&1 | tail -5)
# New tests should be ADDED, no tests should be REMOVED
```

---

## 8. Summary: What MUST Be True Before Each Merge

| Checkpoint | Criteria |
|------------|----------|
| Every stream | `pytest tests/ci/ -v` passes with 0 failures |
| Every stream | `ruff check . && ruff format --check .` passes |
| Every stream | Stream-specific new tests pass |
| Every stream | Manual integration validation done and documented |
| S2 specifically | Desktop backward-compat shim deployed first |
| S2 specifically | Cross-repo JSONL validator passes |
| S5 specifically | ALL `tests/ci/browser/` tests pass (zero tolerance) |
| S6 specifically | Chromium-only fallback works when Camoufox missing |
| Final merge | Full test suite count >= baseline count (tests added, not removed) |
| Final merge | Anti-detection baseline re-captured and compared |
