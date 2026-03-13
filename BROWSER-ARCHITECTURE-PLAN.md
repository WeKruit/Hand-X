# Hand-X Browser Architecture Implementation Plan

## Overview

This plan implements two decisions from the architecture debate:

1. **Browser Engine: Hybrid (Option D)** -- Phased from enhanced Chromium with JS injection stealth layer, through a BrowserProvider abstraction, to Firefox (Camoufox) engine support and route-based hybrid engine selection.

2. **Browser Lifecycle: Lease Protocol (Option C)** -- Fix the `keep_alive` unconditional kill bug, normalize the JSONL schema (`type` -> `event`), add lease acquire/release/heartbeat messages, wire HITL into the executor, and add Desktop lease handler with state refresh on resume.

**Repos touched:** `Hand-X` (primary), `GH-Desktop-App` (lease protocol + JSONL normalization)

**CRITICAL:** All GhostHands application code (`ghosthands/`) has **zero test coverage**. The 807 existing tests only cover the upstream `browser_use/` library. Stream 0 (baseline tests) must complete before any changes begin.

---

## 1. Work Streams

Seven work streams over 7 weeks. Stream 0 is a prerequisite. Streams 1-3 can begin simultaneously after S0.

```
Week 0:    [S0: Baseline Tests — PREREQUISITE]
Week 1-2:  [S1: Stealth Layer]  [S2: JSONL + Lease Protocol]  [S3: keep_alive + HITL Fix]
Week 3:    [S1 continues]       [S2 continues]                 [S4: Domain Lockdown Wiring]
Week 4:    [S5: BrowserProvider Abstraction]
Week 5-6:  [S6: Camoufox Engine + Route-Based Selection]
```

---

## 2. Per-Stream Details

---

### Stream 0: Baseline Test Coverage (PREREQUISITE)

**Branch:** `feat/handx-baseline-tests`

**Goal:** Write tests for the current behavior of every GhostHands file we plan to change. These tests establish the "green baseline" — they must pass before AND after each stream's changes (with intentional modifications where the stream changes behavior).

**Why this is non-negotiable:** The test audit found that `ghosthands/output/jsonl.py`, `ghosthands/agent/factory.py`, `ghosthands/worker/executor.py`, `ghosthands/worker/hitl.py`, and `ghosthands/cli.py` have **zero test coverage**. Without baseline tests, we cannot detect regressions.

#### Files to Create

| File | Covers | Key Assertions |
|------|--------|---------------|
| `tests/ci/test_jsonl_output.py` | `emit_event()`, all `emit_*()` helpers, `install_stdout_guard()` | Output format, key names, timestamp presence, stdout vs stderr routing |
| `tests/ci/test_agent_factory_lifecycle.py` | `create_job_agent()`, `run_job_agent()` | Agent creation params, `keep_alive` behavior (documents current unconditional kill as known behavior), browser cleanup |
| `tests/ci/test_hitl_manager.py` | `HITLManager.pause_job()`, `wait_for_resume()`, `_poll_for_resume()` | Mock asyncpg, state transitions, timeout behavior, poll fallback |
| `tests/ci/test_cli_args.py` | `parse_args()`, BrowserProfile construction from CLI args | Arg parsing, profile field mapping, default values |
| `tests/ci/test_domain_lockdown_integration.py` | CLI path + DomainLockdown interaction | Documents that CLI currently has NO domain filtering (gap S4 fixes) |

#### Acceptance Criteria

- [ ] All new baseline tests pass against the CURRENT codebase (no changes)
- [ ] CI auto-discovers all tests (files under `tests/ci/` with `test_` prefix)
- [ ] Tests use `mock_llm` and `pytest-httpserver` fixtures — no real API keys or DB needed
- [ ] Each test documents its expected behavior so future streams can update assertions intentionally
- [ ] Full `pytest tests/ci/` suite still passes (no interference with existing 807 tests)

#### Dependencies

None — this is the first stream.

#### Estimated Effort

**3 days**

---

### Stream 1: JS Injection Stealth Layer

**Branch:** `feat/handx-stealth-layer`

**Goal:** Inject anti-detection JavaScript into every page via `Page.addScriptToEvaluateOnNewDocument`, making Chromium appear as a regular user browser to ATS bot detection systems.

#### Files to Create

| File | Purpose |
|------|---------|
| `browser_use/browser/stealth/__init__.py` | Package init, exports `StealthConfig`, `get_stealth_scripts()` |
| `browser_use/browser/stealth/scripts.py` | All stealth JS payloads as Python string constants |
| `browser_use/browser/stealth/config.py` | `StealthConfig` pydantic model (enable/disable individual patches) |
| `browser_use/browser/watchdogs/stealth_watchdog.py` | Watchdog that injects stealth scripts on `BrowserConnectedEvent` |
| `tests/ci/security/test_stealth_injection.py` | Tests: scripts injected, `navigator.webdriver` is false, Chrome runtime present |

#### Files to Modify

| File | Change | Location |
|------|--------|----------|
| `browser_use/browser/profile.py` | Add `stealth: StealthConfig` field to `BrowserProfile` | After line 606 (`captcha_solver` field) |
| `browser_use/browser/profile.py` | Add `'--disable-blink-features=AutomationControlled'` to `CHROME_DEFAULT_ARGS` | Line ~175, in the `CHROME_DEFAULT_ARGS` list |
| `browser_use/browser/session.py` | Register `StealthWatchdog` in watchdog setup | In the `_setup_watchdogs()` method (search for where other watchdogs are registered) |
| `browser_use/browser/events.py` | No changes needed -- reuse `BrowserConnectedEvent` |

#### Stealth Scripts to Implement (in `scripts.py`)

Each script is a JS string injected via `Page.addScriptToEvaluateOnNewDocument`:

1. **`WEBDRIVER_PATCH`** -- `Object.defineProperty(navigator, 'webdriver', {get: () => undefined})` and delete `navigator.__proto__.webdriver`
2. **`CHROME_RUNTIME_PATCH`** -- Create `window.chrome.runtime` stub with `sendMessage`, `connect` methods
3. **`PLUGINS_PATCH`** -- Spoof `navigator.plugins` with 3-5 common plugins (Chrome PDF Viewer, etc.)
4. **`LANGUAGES_PATCH`** -- Ensure `navigator.languages` returns `['en-US', 'en']` instead of empty
5. **`PERMISSIONS_PATCH`** -- Override `Notification.permission` to return `'default'` and patch `navigator.permissions.query` for `notifications`
6. **`WEBGL_PATCH`** -- Spoof WebGL renderer/vendor strings to match a real GPU
7. **`HAIRLINE_PATCH`** -- Add `hairline` CSS class to `<html>` for retina device detection
8. **`IFRAME_CONTENTWINDOW_PATCH`** -- Patch `HTMLIFrameElement.prototype.contentWindow` to avoid null on cross-origin iframes
9. **`MEDIA_CODECS_PATCH`** -- Ensure `MediaSource.isTypeSupported()` returns true for common codecs

#### StealthWatchdog Implementation

```python
class StealthWatchdog(BaseWatchdog):
    LISTENS_TO = [BrowserConnectedEvent]

    async def on_BrowserConnectedEvent(self, event):
        if not self.browser_session.browser_profile.stealth.enabled:
            return
        config = self.browser_session.browser_profile.stealth
        for script in get_stealth_scripts(config):
            await self.browser_session._cdp_add_init_script(script)
```

This leverages the existing `_cdp_add_init_script()` at `session.py:3281-3289` which wraps `Page.addScriptToEvaluateOnNewDocument`.

#### Acceptance Criteria

- [ ] `navigator.webdriver` returns `undefined` (not `true`) on launched pages
- [ ] `window.chrome.runtime` exists and has `sendMessage` method
- [ ] `navigator.plugins.length >= 3`
- [ ] Stealth scripts survive navigation (injected via `addScriptToEvaluateOnNewDocument`, not one-shot `evaluate`)
- [ ] Stealth scripts survive iframe creation
- [ ] `StealthConfig(enabled=False)` skips all injection
- [ ] Individual patches can be disabled: `StealthConfig(webdriver_patch=False)`
- [ ] No regression on existing browser tests (`tests/ci/browser/`)

#### Dependencies

None -- this stream has no dependencies on other streams.

#### Estimated Effort

**5 days**

---

### Stream 2: JSONL Schema Normalization + Lease Protocol Messages

**Branch:** `feat/handx-lease-protocol`

**Goal:** Fix the `type` vs `event` key mismatch between Hand-X JSONL output and Desktop app expectations. Add lease acquire/release/heartbeat message types to the JSONL protocol.

#### The Bug

Hand-X `ghosthands/output/jsonl.py` line 68-69 emits:
```json
{"type": "status", "timestamp": 1234567890, "message": "..."}
```

Desktop `GH-Desktop-App/src/main/runHandX.ts` line 1279 expects:
```json
{"event": "status", ...}
```

The Desktop checks `(parsed as { event?: unknown }).event !== 'string'` and drops any line without an `event` key as malformed. Currently this means **all Hand-X JSONL output is silently discarded as malformed**.

#### Files to Modify (Hand-X)

| File | Change | Location |
|------|--------|----------|
| `ghosthands/output/jsonl.py` | Change `emit_event()` to emit `"event"` key instead of `"type"` key | Line 68: change `"type": event_type` to `"event": event_type` |
| `ghosthands/output/jsonl.py` | Add `emit_lease_acquired()`, `emit_lease_released()`, `emit_lease_heartbeat()`, `emit_browser_ready()` | After line 177 (end of file) |
| `ghosthands/output/jsonl.py` | Add `emit_handshake()` for protocol version negotiation | After line 177 |
| `ghosthands/cli.py` | Emit `handshake` event at startup (after stdout guard install) | Line 158, before first `emit_status` call |
| `ghosthands/cli.py` | Emit `browser_ready` event with CDP URL after browser session starts | Line 235-236, after `BrowserSession` creation |
| `ghosthands/cli.py` | Emit `lease_acquired` at start, `lease_released` on exit | Line 158 (after init) and line 623 (in finally block of `_wait_for_review_command`) |
| `ghosthands/cli.py` | Wire `--allowed-domains` CLI argument | Line 97 area (in `parse_args`), add argument |
| `ghosthands/cli.py` | Pass `allowed_domains` to `BrowserProfile` constructor | Line 227-234, add to `BrowserProfile()` kwargs |

#### New JSONL Event Types

```python
def emit_handshake(protocol_version: int = 2) -> None:
    """Emit protocol handshake as the very first event."""
    emit_event("handshake", protocol_version=protocol_version)

def emit_browser_ready(cdp_url: str) -> None:
    """Emit when the browser is connected and CDP is available."""
    emit_event("browser_ready", cdpUrl=cdp_url)

def emit_lease_acquired(lease_id: str, job_id: str = "") -> None:
    """Emit when a lease is acquired from the Desktop app."""
    emit_event("lease_acquired", leaseId=lease_id, jobId=job_id or None)

def emit_lease_released(lease_id: str, reason: str = "completed") -> None:
    """Emit when a lease is released (agent done or cancelled)."""
    emit_event("lease_released", leaseId=lease_id, reason=reason)

def emit_lease_heartbeat(lease_id: str) -> None:
    """Emit periodic lease heartbeat to indicate the process is alive."""
    emit_event("lease_heartbeat", leaseId=lease_id)
```

#### Files to Modify (GH-Desktop-App)

| File | Change | Location |
|------|--------|----------|
| `src/main/runHandX.ts` | Add `HandXLeaseAcquiredEvent`, `HandXLeaseReleasedEvent`, `HandXLeaseHeartbeatEvent` types | After line 109 (after `HandXHandshakeEvent`) |
| `src/main/runHandX.ts` | Add lease event types to `HandXEvent` union | Line 111-122 |
| `src/main/runHandX.ts` | Handle `lease_acquired`, `lease_released`, `lease_heartbeat` in `handleEvent` switch | Line 1136, in the switch statement |
| `src/main/localWorkerHost.ts` | Track lease state from Hand-X events, implement lease timeout | In the job orchestration section |

#### Acceptance Criteria

- [ ] Hand-X emits `{"event": "status", ...}` (not `{"type": "status", ...}`)
- [ ] Desktop no longer drops Hand-X events as malformed
- [ ] Protocol handshake event emitted as the first line on stdout
- [ ] `browser_ready` event emitted with CDP URL after browser connects
- [ ] `lease_acquired` event emitted when agent starts
- [ ] `lease_released` event emitted when agent finishes (success or failure)
- [ ] Heartbeat events emitted every 15 seconds during agent execution
- [ ] All existing JSONL event types still work: `status`, `field_filled`, `field_failed`, `progress`, `cost`, `done`, `error`
- [ ] Desktop app correctly processes all event types end-to-end

#### Dependencies

None -- can proceed independently.

#### Estimated Effort

**4 days** (2 Hand-X, 2 Desktop)

---

### Stream 3: `keep_alive` Unconditional Kill Fix + HITL Wiring

**Branch:** `feat/handx-lifecycle-fix`

**Goal:** Fix the bug where the worker unconditionally kills the browser after agent completion (ignoring `keep_alive=True`), and wire the existing `HITLManager` into the executor's agent run loop.

#### The Bug

In `ghosthands/agent/factory.py` line 259-268, `run_job_agent()` always calls `agent.browser_session.kill()` in its `finally` block, regardless of `keep_alive=True` set at line 131. The browser-use `Agent.close()` at `browser_use/agent/service.py` line 3925-3944 correctly respects `keep_alive`, but the GhostHands wrapper bypasses this by calling `kill()` directly.

Meanwhile, `HITLManager` is created at `ghosthands/worker/executor.py` line 144 but **never used** -- it's passed to `_run_agent()` at line 228 but `_run_agent()` never calls `hitl.pause_job()` or `hitl.wait_for_resume()`.

#### Files to Modify

| File | Change | Location |
|------|--------|----------|
| `ghosthands/agent/factory.py` | In `run_job_agent()`, respect `keep_alive` in the finally block -- only kill if `keep_alive is False` | Lines 259-268: change `await agent.browser_session.kill()` to check `agent.browser_session.browser_profile.keep_alive` |
| `ghosthands/agent/factory.py` | Add optional `hitl: HITLManager | None` parameter to `run_job_agent()` | Line 182 signature |
| `ghosthands/agent/factory.py` | Pass HITL manager to step hooks for blocker detection | Lines 210-213, pass to `StepHooks` |
| `ghosthands/agent/hooks.py` | Add HITL integration to `StepHooks.on_step_end()` -- detect blockers and call `hitl.pause_job()` | In the `on_step_end` method |
| `ghosthands/worker/executor.py` | Pass `hitl` to `run_job_agent()` call | Line 486-496, add `hitl=hitl` kwarg |
| `ghosthands/worker/executor.py` | After `run_job_agent()` returns, check if result has blocker and trigger HITL wait | Lines 498-513, add HITL pause/wait logic |

#### keep_alive Fix (factory.py)

Current (lines 259-268):
```python
finally:
    if agent.browser_session is not None:
        try:
            await agent.browser_session.kill()
        except Exception:
            pass
```

Fixed:
```python
finally:
    if agent.browser_session is not None:
        try:
            if agent.browser_session.browser_profile.keep_alive is False:
                await agent.browser_session.kill()
            else:
                # keep_alive=True or None: stop event bus but leave browser open
                await agent.browser_session.event_bus.stop(clear=False, timeout=1.0)
        except Exception:
            pass
```

This matches the behavior in `browser_use/agent/service.py` lines 3925-3944.

#### HITL Wiring (executor.py)

After `_run_agent()` returns at line 496, add:

```python
# If the agent reported a blocker, pause for HITL
if result.get("blocker"):
    log.info("executor.hitl_pause", blocker=result["blocker"])
    await hitl.pause_job(
        job_id=job_id,
        reason=result["blocker"],
        interaction_type="blocker",
        valet_task_id=valet_task_id,
    )
    signal = await hitl.wait_for_resume(job_id)
    if signal and signal.get("action") == "cancel":
        return {"success": False, "error": "Cancelled by user", "error_code": "user_cancelled"}
    # On resume, result is already captured -- continue to completion
```

#### Acceptance Criteria

- [ ] When `keep_alive=True` and agent completes, browser window stays open
- [ ] When `keep_alive=False`, browser is killed as before
- [ ] Worker executor calls `hitl.pause_job()` when agent reports a blocker
- [ ] Worker executor waits via `hitl.wait_for_resume()` for human signal
- [ ] Cancel signal from HITL correctly stops the job
- [ ] Resume signal from HITL correctly continues processing
- [ ] No change to CLI path behavior (`ghosthands/cli.py` already handles `keep_alive=True` correctly)

#### Dependencies

None -- can proceed independently.

#### Estimated Effort

**3 days**

---

### Stream 4: Domain Lockdown Wiring into CLI Path

**Branch:** `feat/handx-domain-lockdown-cli`

**Goal:** Wire the existing `DomainLockdown` class (at `ghosthands/security/domain_lockdown.py`) into the CLI path. Currently it exists but is **not wired** -- `cli.py` line 227 creates a `BrowserProfile` without `allowed_domains`, and the `DomainLockdown` class is never instantiated in the CLI flow.

#### Files to Modify

| File | Change | Location |
|------|--------|----------|
| `ghosthands/cli.py` | Add `--allowed-domains` CLI argument (comma-separated list) | Line 97, in `parse_args()` |
| `ghosthands/cli.py` | Create `DomainLockdown` instance from `--job-url` and detected platform | Line 204 area, after platform detection |
| `ghosthands/cli.py` | Pass `allowed_domains` to `BrowserProfile` constructor | Lines 227-234, add `allowed_domains=lockdown.get_allowed_domains()` |
| `ghosthands/cli.py` | In `run_agent_human()`, also wire domain lockdown | Lines 426-433, same change |
| `ghosthands/security/domain_lockdown.py` | Add method to export as `BrowserProfile`-compatible list | After line 248, add `def to_browser_profile_domains() -> list[str]` |

#### Acceptance Criteria

- [ ] CLI agent respects domain lockdown for the detected platform
- [ ] `--allowed-domains "example.com,other.com"` adds extra domains
- [ ] Navigation to non-allowed domains is blocked by browser-use's built-in domain filtering
- [ ] Existing ATS-specific allowlists (Workday, Greenhouse, Lever) are automatically included

#### Dependencies

Depends on Stream 2 (JSONL normalization) only if `--allowed-domains` is part of the lease protocol. Can proceed independently if scoped to CLI-only.

#### Estimated Effort

**2 days**

---

### Stream 5: BrowserProvider Abstraction

**Branch:** `feat/handx-browser-provider`

**Goal:** Create an abstraction layer (`BrowserProvider`) that decouples the agent from the specific browser engine. This enables swapping Chromium for Camoufox (Firefox) without changing agent code.

#### Files to Create

| File | Purpose |
|------|---------|
| `browser_use/browser/providers/__init__.py` | Package init, exports `BrowserProvider`, `ChromiumProvider` |
| `browser_use/browser/providers/base.py` | `BrowserProvider` abstract base class |
| `browser_use/browser/providers/chromium.py` | `ChromiumProvider` -- wraps current `LocalBrowserWatchdog` behavior |
| `browser_use/browser/providers/registry.py` | `ProviderRegistry` -- maps engine names to provider classes |

#### BrowserProvider Interface

```python
from abc import ABC, abstractmethod

class BrowserProvider(ABC):
    """Abstract browser engine provider."""

    @abstractmethod
    async def launch(self, profile: BrowserProfile) -> tuple[str, int | None]:
        """Launch browser and return (cdp_url, pid).

        Returns:
            Tuple of (cdp_url, process_pid_or_none)
        """
        ...

    @abstractmethod
    async def kill(self) -> None:
        """Kill the browser process."""
        ...

    @abstractmethod
    def get_default_args(self, profile: BrowserProfile) -> list[str]:
        """Get engine-specific launch arguments."""
        ...

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Return engine identifier: 'chromium' or 'firefox'."""
        ...

    @property
    @abstractmethod
    def supports_cdp(self) -> bool:
        """Whether this engine supports CDP protocol."""
        ...
```

#### Files to Modify

| File | Change | Location |
|------|--------|----------|
| `browser_use/browser/profile.py` | Add `engine: Literal['chromium', 'firefox', 'auto'] = 'chromium'` field to `BrowserProfile` | After line 580 (`disable_security` field) |
| `browser_use/browser/watchdogs/local_browser_watchdog.py` | Refactor `_launch_browser()` to delegate to `ChromiumProvider` | Lines 93-217, extract browser-finding and launching logic |
| `browser_use/browser/session.py` | Add `provider` property that resolves engine from profile | Near the top of the class, after `__init__` |

#### Acceptance Criteria

- [ ] `BrowserProfile(engine='chromium')` produces identical behavior to current code
- [ ] `ChromiumProvider` passes all existing browser tests
- [ ] `ProviderRegistry.get('chromium')` returns `ChromiumProvider`
- [ ] `ProviderRegistry.get('firefox')` raises `NotImplementedError` (placeholder for Stream 6)
- [ ] No regression on any existing tests

#### Dependencies

Depends on Stream 1 (stealth layer) being merged, because the stealth watchdog needs to be engine-aware (some patches are Chrome-specific).

#### Estimated Effort

**4 days**

---

### Stream 6: Camoufox Engine Adapter + Route-Based Selection

**Branch:** `feat/handx-camoufox-engine`

**Goal:** Add Firefox (Camoufox) as an alternative browser engine behind the `BrowserProvider` abstraction, and implement route-based engine selection per target site.

#### Files to Create

| File | Purpose |
|------|---------|
| `browser_use/browser/providers/camoufox.py` | `CamoufoxProvider` -- launches Camoufox (Firefox fork with anti-detect) via CDP-over-Firefox |
| `browser_use/browser/providers/route_selector.py` | `RouteSelector` -- maps target URL patterns to engine choice |
| `browser_use/browser/stealth/firefox_scripts.py` | Firefox-specific stealth patches (different from Chromium) |
| `tests/ci/browser/test_camoufox_provider.py` | Tests for Camoufox engine launch, CDP connectivity, stealth |
| `tests/ci/browser/test_route_selector.py` | Tests for URL-to-engine routing |

#### CamoufoxProvider Implementation

Camoufox is a Firefox fork with built-in anti-detect. It exposes a CDP-compatible interface via the Marionette->CDP bridge. Key differences from Chromium:

- Binary path: `camoufox` command or `~/.camoufox/camoufox` binary
- Launch args: Firefox-style (`-headless`, `-profile`, not `--headless`, `--user-data-dir`)
- CDP: Exposed via `--remote-debugging-port` (same as Chrome)
- Stealth: Most anti-detect is built in; fewer JS patches needed

#### RouteSelector Implementation

```python
class RouteSelector:
    """Select browser engine based on target URL."""

    # Sites known to have aggressive Chromium detection
    FIREFOX_PREFERRED: dict[str, list[str]] = {
        "workday": ["myworkdayjobs.com", "myworkday.com", "workday.com"],
    }

    # Sites known to work better with Chromium
    CHROMIUM_PREFERRED: dict[str, list[str]] = {
        "greenhouse": ["greenhouse.io"],
        "lever": ["lever.co"],
    }

    def select_engine(self, url: str, platform: str = "") -> str:
        """Return 'chromium' or 'firefox' based on URL/platform."""
        ...
```

#### Files to Modify

| File | Change | Location |
|------|--------|----------|
| `browser_use/browser/providers/registry.py` | Register `CamoufoxProvider` | In the registry initialization |
| `browser_use/browser/profile.py` | Add `'firefox'` to `engine` field's `Literal` union | Where `engine` field is defined (from Stream 5) |
| `browser_use/browser/session.py` | Use `RouteSelector` when `engine='auto'` | In the provider resolution logic (from Stream 5) |
| `ghosthands/agent/factory.py` | Add `engine` parameter to `create_job_agent()` | Line 41 signature, pass through to `BrowserProfile` |
| `ghosthands/cli.py` | Add `--engine` CLI argument (`chromium`, `firefox`, `auto`) | In `parse_args()` |

#### Acceptance Criteria

- [ ] `BrowserProfile(engine='firefox')` launches Camoufox and connects via CDP
- [ ] Agent can navigate, fill forms, and take screenshots in Camoufox
- [ ] `BrowserProfile(engine='auto')` selects engine based on target URL
- [ ] Workday URLs route to Firefox, Greenhouse/Lever route to Chromium
- [ ] Stealth scripts adapt to engine (Chrome-specific patches skipped in Firefox)
- [ ] Graceful fallback: if Camoufox is not installed, fall back to Chromium with warning

#### Dependencies

Depends on Stream 5 (BrowserProvider abstraction) being merged.

#### Estimated Effort

**8 days**

---

## 3. Worktree Management

Each stream uses a dedicated git worktree to enable parallel development:

```bash
# From Hand-X repo root
cd "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X"

# Create worktrees for independent streams (weeks 1-2)
git worktree add .claude/worktrees/feat-stealth-layer feat/handx-stealth-layer
git worktree add .claude/worktrees/feat-lease-protocol feat/handx-lease-protocol
git worktree add .claude/worktrees/feat-lifecycle-fix feat/handx-lifecycle-fix

# Create worktrees for dependent streams (weeks 3+)
git worktree add .claude/worktrees/feat-domain-lockdown-cli feat/handx-domain-lockdown-cli
git worktree add .claude/worktrees/feat-browser-provider feat/handx-browser-provider
git worktree add .claude/worktrees/feat-camoufox-engine feat/handx-camoufox-engine
```

For the Desktop app changes in Stream 2:
```bash
cd "/Users/adam/Desktop/WeKruit/VALET & GH/GH-Desktop-App"
git worktree add .claude/worktrees/feat-lease-protocol feat/desktop-lease-protocol
```

**Worktree rules:**
- Each worktree has its own `.venv` (run `uv venv && uv pip install -e ".[dev]"` in each)
- Never modify the same file in two active worktrees simultaneously
- Merge dependent streams in order (see Section 6)
- Delete worktrees after merge: `git worktree remove .claude/worktrees/<name>`

---

## 4. Testing Strategy

### 4.1 Anti-Detection Testing (Stream 1)

**Automated tests** (`tests/ci/security/test_stealth_injection.py`):

```python
async def test_webdriver_not_detectable():
    """navigator.webdriver must be undefined after stealth injection."""
    profile = BrowserProfile(stealth=StealthConfig(enabled=True))
    session = BrowserSession(browser_profile=profile)
    await session.start()
    page = await session.get_current_page()
    result = await page.evaluate("navigator.webdriver")
    assert result is None or result is False  # undefined coerces to None in Python

async def test_chrome_runtime_exists():
    """window.chrome.runtime must exist."""
    ...
    result = await page.evaluate("typeof window.chrome?.runtime?.sendMessage")
    assert result == "function"

async def test_plugins_populated():
    """navigator.plugins must have entries."""
    ...
    count = await page.evaluate("navigator.plugins.length")
    assert count >= 3

async def test_stealth_survives_navigation():
    """Stealth patches must persist across page navigations."""
    ...
    await page.goto("https://example.com")
    result_before = await page.evaluate("navigator.webdriver")
    await page.goto("https://example.org")
    result_after = await page.evaluate("navigator.webdriver")
    assert result_before is None
    assert result_after is None
```

**Manual validation against detection sites:**
1. [https://bot.sannysoft.com](https://bot.sannysoft.com) -- comprehensive bot detection
2. [https://arh.antoinevastel.com/bots/areyouheadless](https://arh.antoinevastel.com/bots/areyouheadless) -- headless detection
3. [https://abrahamjuliot.github.io/creepjs/](https://abrahamjuliot.github.io/creepjs/) -- fingerprint analysis
4. Run against a real Workday staging site with known bot detection

**Regression tests:** All existing `tests/ci/browser/` tests must pass unchanged.

### 4.2 Lease Protocol Testing (Stream 2)

**Unit tests** for JSONL output:
```python
def test_emit_event_uses_event_key():
    """emit_event must use 'event' key, not 'type'."""
    import io, json
    buf = io.StringIO()
    # Redirect output to buffer
    emit_event("status", message="test")
    line = buf.getvalue().strip()
    obj = json.loads(line)
    assert "event" in obj
    assert "type" not in obj
    assert obj["event"] == "status"
```

**Integration test** with Desktop app:
1. Spawn Hand-X subprocess from test harness
2. Read stdout JSONL
3. Verify first line is `{"event": "handshake", "protocol_version": 2, ...}`
4. Verify subsequent lines all have `"event"` key
5. Verify lease lifecycle: `lease_acquired` -> `status` events -> `done` -> `lease_released`

**Desktop-side tests** (`GH-Desktop-App/src/main/__tests__/run-hand-x-integration.test.ts`):
- Verify `handleEvent` correctly processes all event types
- Verify lease state transitions
- Verify heartbeat timeout detection

### 4.3 Lifecycle Testing (Stream 3)

**Unit tests:**
```python
async def test_keep_alive_true_preserves_browser():
    """When keep_alive=True, run_job_agent must NOT kill the browser."""
    # Create agent with keep_alive=True, run with mock task, verify browser PID still exists

async def test_keep_alive_false_kills_browser():
    """When keep_alive=False, run_job_agent must kill the browser."""
    # Create agent with keep_alive=False, run, verify browser PID is gone

async def test_hitl_pause_on_blocker():
    """Executor must call hitl.pause_job when agent reports a blocker."""
    # Mock HITLManager, run executor with blocker result, verify pause_job called
```

### 4.4 End-to-End Validation

After all streams merge, run a full end-to-end test:
1. Start Desktop app
2. Dispatch a job to Hand-X via local worker
3. Verify JSONL events flow correctly (handshake -> lease_acquired -> status -> field_filled -> done -> lease_released)
4. Verify browser stays open for review (keep_alive=True)
5. Send `complete_review` command from Desktop
6. Verify browser closes and lease is released
7. Verify stealth patches are active in the browser during execution

---

## 5. Risk Mitigation

### Risk 1: Stealth scripts break existing functionality
- **Mitigation:** `StealthConfig(enabled=True)` is the default, but each patch can be individually disabled. If a specific patch causes issues on a platform (e.g., WebGL patch breaks Workday), disable it per-platform in `RouteSelector`.
- **Contingency:** Ship with `StealthConfig(enabled=False)` as default and opt-in per-platform.

### Risk 2: JSONL schema change breaks Desktop app
- **Mitigation:** Both Hand-X and Desktop changes ship in the same release. The handshake event (`protocol_version: 2`) enables Desktop to detect and adapt to the new schema.
- **Contingency:** Add backward-compatibility shim in Desktop that accepts both `type` and `event` keys during transition:
  ```typescript
  const eventKey = (parsed as any).event ?? (parsed as any).type;
  if (typeof eventKey !== 'string') { /* malformed */ }
  ```

### Risk 3: Camoufox CDP compatibility gaps
- **Mitigation:** Phase 6 (Camoufox) is last in the merge order. If Camoufox's CDP support is insufficient for browser-use's needs (missing DOM methods, incomplete `Page` domain), the `BrowserProvider` abstraction still ships and `engine='auto'` falls back to Chromium.
- **Contingency:** Keep Camoufox behind a feature flag (`GH_ENABLE_FIREFOX=true`). Default to Chromium-only.

### Risk 4: keep_alive fix causes browser process leaks
- **Mitigation:** The fix aligns `run_job_agent()` with the behavior already implemented in `Agent.close()`. Add a cleanup timer: if browser is kept alive but no review command arrives within 30 minutes, kill it.
- **Contingency:** Add `GH_FORCE_KILL_ON_COMPLETE=true` env var to restore old behavior.

### Risk 5: HITL wiring introduces async deadlocks in executor
- **Mitigation:** `hitl.wait_for_resume()` has a 5-minute timeout (`DEFAULT_WAIT_TIMEOUT = 300.0` at `hitl.py` line 27). The heartbeat loop at `executor.py` lines 70-82 runs independently and keeps the job alive during HITL wait.
- **Contingency:** If HITL wait deadlocks the executor, fall back to the existing behavior (skip HITL, fail the job on blocker).

### Risk 6: BrowserProvider refactor breaks LocalBrowserWatchdog
- **Mitigation:** `ChromiumProvider` is a thin wrapper that delegates to the exact same code paths in `LocalBrowserWatchdog`. Extract, don't rewrite. Run the full browser test suite after extraction.
- **Contingency:** Keep `LocalBrowserWatchdog` unchanged and have `ChromiumProvider` compose it rather than replace it.

---

## 6. Merge Order (Dependency Graph)

```
                    ┌─────────────────────┐
                    │  S1: Stealth Layer   │
                    │  (no deps)           │
                    └──────────┬──────────┘
                               │
                               ▼
┌──────────────────┐   ┌──────────────────────┐
│ S2: JSONL/Lease  │   │ S5: BrowserProvider  │
│ (no deps)        │   │ (depends on S1)      │
└────────┬─────────┘   └──────────┬───────────┘
         │                        │
         ▼                        ▼
┌──────────────────┐   ┌──────────────────────┐
│ S4: Domain Lock  │   │ S6: Camoufox/Route   │
│ (depends on S2)  │   │ (depends on S5)      │
└──────────────────┘   └──────────────────────┘

┌──────────────────┐
│ S3: Lifecycle    │
│ (no deps)        │
└──────────────────┘
```

### Merge Sequence

| Order | Stream | Branch | Merge Into | Gate |
|-------|--------|--------|------------|------|
| **0th** | **S0: Baseline Tests** | `feat/handx-baseline-tests` | `main` | **All new tests pass against current code, full CI green** |
| 1st | S2: JSONL + Lease Protocol | `feat/handx-lease-protocol` + `feat/desktop-lease-protocol` | `main` (both repos) | S0 JSONL tests updated + pass, Desktop integration test passes |
| 2nd | S3: Lifecycle Fix | `feat/handx-lifecycle-fix` | `main` | S0 factory/HITL tests updated + pass |
| 3rd | S1: Stealth Layer | `feat/handx-stealth-layer` | `main` | All stealth tests pass, no regression on `tests/ci/browser/` |
| 4th | S4: Domain Lockdown CLI | `feat/handx-domain-lockdown-cli` | `main` | S0 CLI tests updated + pass |
| 5th | S5: BrowserProvider | `feat/handx-browser-provider` | `main` | All browser tests pass with `ChromiumProvider` |
| 6th | S6: Camoufox + Route | `feat/handx-camoufox-engine` | `main` | Camoufox tests pass (or skip if Camoufox not installed in CI) |

**Rationale:**
- S2 merges first because the `type` -> `event` fix is a critical correctness bug that affects all Desktop<->Hand-X communication.
- S3 merges second because the `keep_alive` fix is needed for the lease protocol to work correctly (lease_released must happen after browser stays open for review).
- S1 merges third because it's the foundation for S5's engine-aware stealth.
- S4 is a small, isolated change that can merge anytime after S2.
- S5 and S6 are the largest structural changes and merge last.

### Coordinated Release

S2 requires synchronized changes in both Hand-X and GH-Desktop-App. The approach:

1. Merge Hand-X `feat/handx-lease-protocol` first (Hand-X emits `event` key)
2. Merge Desktop `feat/desktop-lease-protocol` immediately after (Desktop already handles both `event` and `type` via the backward-compat shim)
3. Ship Desktop update with the new event handling
4. After Desktop rollout confirms stable, remove the backward-compat shim in a follow-up PR

---

## 7. Edge Cases & Architectural Gaps (from review)

These gaps were identified during the architecture review and must be addressed within the relevant streams.

### 7.1 Lease Protocol: Two-Phase Handoff (Stream 2)

**Problem:** CDP allows multiple debug clients. If both Desktop (Playwright `connectOverCDP`) and Hand-X (`CDPClient`) send commands simultaneously during handoff, results are undefined.

**Required:** A two-phase handoff protocol:
1. Current holder signals "releasing" → calls `BrowserSession.stop()` (clears event buses, keeps browser alive)
2. New holder confirms "acquired" → no CDP commands legal in the gap
3. Desktop must `browser.close()` its Playwright handle before signaling release
4. `reconnect()` in `session.py:2005` must guard against firing while Desktop still holds lease

### 7.2 Page Drift on HITL Resume (Stream 3)

**Problem:** User may navigate to a different page/domain during HITL pause. On resume, `AgentState` stores stale `last_model_output`, `plan`, and DOM references.

**Required:** On lease re-acquire:
1. Take fresh DOM snapshot via `get_state()` pipeline
2. Compare current URL against pre-pause URL
3. If URL changed: inject synthetic `ActionResult` ("Page changed during human intervention")
4. If tab closed: use `reconnect()` to find/create new tab, navigate back
5. Clear or mark `last_model_output` as "pre-pause" to prevent stale action replay
6. Validate current URL against `allowed_domains`

### 7.3 Network Drain Before Lease Release (Stream 3)

**Problem:** Agent may pause mid-form-submission with in-flight XHR/fetch requests.

**Required:** Before signaling `lease_released`:
1. Wait for `networkIdle` (use existing `wait_for_network_idle_page_load_time` in BrowserProfile)
2. Current action completes before pause takes effect (already the behavior — `pause()` checks at step boundaries)

### 7.4 Lease Crash Recovery: Heartbeat Timeout (Stream 2)

**Problem:** If Hand-X crashes mid-lease, browser becomes orphaned — nobody holds the lease.

**Required:**
- Heartbeat every 15 seconds (already in the plan)
- Desktop auto-acquires after 30 seconds of silence
- Stale lease detection: if `lease_acquired` was emitted but process exited without `lease_released`, Desktop takes over
- Desktop should monitor Hand-X process exit code as a fast-path signal

### 7.5 Postgres LISTEN/NOTIFY Reliability (Stream 3)

**Problem:** LISTEN connection can die mid-wait (not just at setup). `hitl.py` catches at subscription time (line 163) but not during wait.

**Required:**
- Parallel health-check task that pings connection periodically during `wait_for_resume()`
- Falls back to `_poll_for_resume()` (5-second polling, already exists at lines 213-247)
- **Desktop path gap:** `HandXCommand` in Desktop only supports `cancel` and `complete_review` — no `resume` command exists. Must add `resume` to the stdin command protocol.

### 7.6 Additional JSONL Field Mismatches (Stream 2)

Beyond `type` vs `event`, there are field-level mismatches:

| Event | Hand-X emits | Desktop expects | Fix |
|-------|-------------|-----------------|-----|
| `field_failed` | `error` key | `reason` key | Rename to `reason` in `jsonl.py` |
| `progress` | `filled`, `total` | `step`, `maxSteps`, `description` | Map `filled`→`step`, `total`→`maxSteps`, add `description` |

These are silently swallowed because TypeScript types are compile-time only.

### 7.7 Topology-Aware Factory (Stream 3)

**Problem:** EC2 workers don't need lease protocol, keep_alive, or JSONL IPC. Desktop does.

**Required:** `factory.py` should accept `topology: Literal["desktop", "server"]`:

| Concern | Desktop | EC2 |
|---------|---------|-----|
| Browser launch | Desktop owns, passes `cdpUrl` | Worker launches internally |
| HITL signal | JSONL stdin command | Postgres NOTIFY |
| Browser cleanup | `keep_alive=True` | `keep_alive=False` |
| Output format | JSONL on stdout | HTTP callbacks + DB |
| Lease protocol | Full handoff cycle | No-op |

### 7.8 Stealth Layer Rollback (Stream 1)

**Required:**
- Feature flag: `GH_STEALTH_ENABLED=true` env var (default `false` initially)
- Bypass mechanism: if CAPTCHA wall detected, retry once without stealth, log which site triggered fallback
- All stealth patches in single module (`browser_use/browser/stealth/`) with single entry point
- Desktop can hot-deploy stealth changes via Hand-X binary updates without shipping new Electron release

### 7.9 BrowserProvider: CDP Leakage (Stream 5)

**Chromium-specific assumptions that will leak through the abstraction:**
- `CDPClient`, `CDPSession`, `TargetID`, `SessionID` used directly in `session.py` (lines 14-18, 81-96)
- `cdp_url` as universal browser handle — Firefox uses Marionette/BiDi, different protocol
- Proxy auth via `Fetch.enable` (CDP Fetch domain) — no Firefox equivalent
- DOM extraction via CDP DOM domain — Camoufox needs injected JS via Marionette
- Desktop `chromium.connectOverCDP()` — Firefox needs `firefox.connect()` with different params

**Mitigation:** Wrap raw CDP in domain-specific methods (`navigate()`, `inject_script()`, `capture_network()`) rather than exposing `execute_cdp_command()`. DomHand actions use `page.evaluate()` which is engine-agnostic — this is the biggest win.

### 7.10 Critical Priority Summary

| # | Finding | Stream | Severity |
|---|---------|--------|----------|
| 1 | JSONL `type`/`event` mismatch — all Desktop events dropped | S2 | **P0** |
| 2 | Additional field mismatches (`error`→`reason`, `filled`→`step`) | S2 | **P0** |
| 3 | HITL never wired — blockers crash agent | S3 | **P0** |
| 4 | No `resume` stdin command in Desktop | S2 | **P1** |
| 5 | Two-phase lease handoff missing | S2 | **P1** |
| 6 | Page drift on HITL resume | S3 | **P1** |
| 7 | Heartbeat crash recovery | S2 | **P1** |
| 8 | `keep_alive=True` hardcoded (should vary by topology) | S3 | **P2** |
| 9 | CDP leakage through BrowserProvider | S5 | **P2** |
| 10 | No stealth layer exists | S1 | **P3** (bridge, not permanent) |

---

## Appendix: File Reference Quick Index

### Hand-X Files (all paths relative to `Hand-X/`)

| File | Key Functions/Classes | Lines Referenced |
|------|----------------------|-----------------|
| `browser_use/browser/session.py` | `BrowserSession`, `reconnect()`, `_cdp_add_init_script()` | 2005, 3281-3289 |
| `browser_use/browser/profile.py` | `BrowserProfile`, `get_args()`, `CHROME_DEFAULT_ARGS` | 175, 392, 580-584, 852-930 |
| `browser_use/browser/watchdogs/local_browser_watchdog.py` | `LocalBrowserWatchdog`, `_launch_browser()` | 93-217, 112-115 |
| `browser_use/agent/service.py` | `Agent`, `close()` | 3925-3944 |
| `ghosthands/agent/factory.py` | `create_job_agent()`, `run_job_agent()` | 41-179, 182-269 |
| `ghosthands/worker/executor.py` | `execute_job()`, `_run_agent()` | 88-341, 347-513 |
| `ghosthands/worker/hitl.py` | `HITLManager`, `pause_job()`, `wait_for_resume()` | 30-247 |
| `ghosthands/output/jsonl.py` | `emit_event()`, all `emit_*()` helpers | 61-78 (the bug: line 69) |
| `ghosthands/cli.py` | `parse_args()`, `run_agent_jsonl()`, `_wait_for_review_command()` | 54-97, 149-354, 576-625 |
| `ghosthands/security/domain_lockdown.py` | `DomainLockdown`, `is_allowed()` | 147-277 |
| `browser_use/browser/events.py` | `BrowserConnectedEvent`, `BrowserLaunchEvent` | (event types) |

### GH-Desktop-App Files (all paths relative to `GH-Desktop-App/`)

| File | Key Functions/Types | Lines Referenced |
|------|---------------------|-----------------|
| `src/main/runHandX.ts` | `HandXEvent` types, `handleEvent()`, event parsing | 35-122, 1131-1248, 1259-1295 |
| `src/main/localWorkerHost.ts` | `WorkerCommand`, job orchestration | 1-100 |
| `src/main/reviewBrowserCoordinator.ts` | `ReviewBrowserHandle`, CDP connection | 1-100 |
