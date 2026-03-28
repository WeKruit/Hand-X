# Codebase Concerns

**Analysis Date:** 2026-03-24

---

## Tech Debt

**HITL Resume-After-Pause is Not Implemented:**
- Issue: When the worker pauses a job for human intervention (CAPTCHA, login wall, 2FA), and the human resolves it, the agent resumes database state but does NOT re-enter the agent loop. The browser has already exited the run. The result from before the pause is returned as-is.
- Files: `ghosthands/worker/executor.py:575-579`
- Impact: Any job that triggers HITL pause and then receives a "resume" signal silently returns the pre-pause result, making resume functionally broken in the worker path. Only the desktop CLI path has a different lifecycle.
- Fix approach: After `hitl.wait_for_resume()` returns a non-cancel signal, re-invoke `run_job_agent()` with the same context, picking up where the browser left off (requires `keep_alive=True` or browser session persistence).

**Duplicate Module-Level Lock Declaration in `jsonl.py`:**
- Issue: `_emit_lock = threading.Lock()` and `_pipe_broken = False` are declared twice at module level (lines 60-64), causing a second lock object to shadow the first silently.
- Files: `ghosthands/output/jsonl.py:60-64`
- Impact: No runtime crash, but the first lock is discarded. Any threads that acquired the first lock before module load would be unaffected; new threads use the second object. More significant as a code smell indicating copy-paste without review.
- Fix approach: Remove the duplicate pair (lines 63-64).

**Late Imports to Break Circular References (Pervasive Pattern):**
- Issue: A large number of functions are re-exported as local late imports inside function bodies to avoid circular dependencies between `domhand_fill.py`, `fill_executor.py`, `fill_label_match.py`, `fill_llm_answers.py`, `fill_profile_resolver.py`, and `fill_verify.py`. Every call to these "delegate" functions incurs a fresh Python import lookup inside the function call.
- Files: `ghosthands/dom/fill_llm_answers.py:31-106`, `ghosthands/dom/fill_verify.py:37-60`, `ghosthands/dom/fill_profile_resolver.py:72-79`, `ghosthands/dom/fill_executor.py` (multiple)
- Impact: Performance overhead on hot paths (every field fill); hard to trace call stacks; indicates the module decomposition was done post-hoc without resolving the underlying dependency graph.
- Fix approach: Introduce a shared `ghosthands/dom/fill_types.py` (or `fill_primitives.py`) for types and pure utility functions that all modules can import without circularity. Progressively move shared logic there.

**`domhand_fill.py` and Related Files Are Excessively Large:**
- Issue: Four files form a mega-module with 9,714 combined lines: `domhand_fill.py` (3,073), `fill_profile_resolver.py` (2,434), `fill_executor.py` (2,224), `cli.py` (1,983). `fill_executor.py` alone has 55+ bare `except Exception: pass` blocks — exceptions are silently swallowed throughout.
- Files: `ghosthands/actions/domhand_fill.py`, `ghosthands/dom/fill_profile_resolver.py`, `ghosthands/dom/fill_executor.py`, `ghosthands/cli.py`
- Impact: Every field-fill code path is nearly impossible to reason about independently. Silent exception swallowing hides real failures. Any change to the core fill path risks undetected regressions.
- Fix approach: Continue the extraction already started (fill_executor, fill_label_match, fill_llm_answers, fill_verify were recently split out). Target <500 lines per file. Replace bare `pass` in exception handlers with at minimum `logger.debug(...)`.

**Runtime-Learning State Lives Only in Process Memory:**
- Issue: `ghosthands/runtime_learning.py` maintains seven module-level dicts (`_loaded_aliases`, `_pending_aliases`, `_confirmed_aliases`, `_semantic_cache`, `_domhand_failure_counts`, `_domhand_retry_capped`, `_expected_field_values`). These are populated during a job run and can be exported via `export_runtime_learning_payload()`, but they are never auto-persisted between runs unless `cli.py` explicitly writes them out.
- Files: `ghosthands/runtime_learning.py:87-115`, `ghosthands/cli.py:1601`, `ghosthands/cli.py:1663`
- Impact: Learned question aliases and interaction recipes are lost on process restart (worker restart, crash). The worker path (`executor.py`) does not call `export_runtime_learning_payload()` at all — learning is completely ephemeral in worker mode.
- Fix approach: Add a post-job hook in `executor.py` to persist the payload to a lightweight store (Redis, S3, or a DB table), and load it at worker startup.

**Profile Data Passed via Environment Variable Through Temp File:**
- Issue: Resume PII (full profile JSON) is written to a temp file and communicated to `domhand_fill` via `os.environ["GH_USER_PROFILE_PATH"]`. This is a process-global side effect that would break if two jobs ever ran concurrently in the same process.
- Files: `ghosthands/worker/executor.py:408-417`, `ghosthands/actions/domhand_fill.py:3017-3064`
- Impact: While currently safe because the worker is single-job, any future attempt to run multiple concurrent jobs (e.g., two browser windows) would cause both jobs to read the last-written profile path. Also, profile data remains in `os.environ` for the duration of the run; errors during cleanup leave it set.
- Fix approach: Pass the profile dict directly through the agent's tool context or as a constructor argument to `DomHandFillParams`, eliminating the env-var relay entirely.

---

## Known Bugs

**`GH_DEBUG_AUTH_CREDENTIALS` Logs Plaintext Password to Structured Logger:**
- Symptoms: When `GH_DEBUG_AUTH_CREDENTIALS=1`, `cli.py` logs `email` and `password` fields at `WARNING` level via `structlog`, which routes to any configured log sink including centralized logging systems.
- Files: `ghosthands/cli.py:416-439`
- Trigger: Set `GH_DEBUG_AUTH_CREDENTIALS=1` in a production environment.
- Workaround: The env var must not be set in production. However, there is no enforcement.

**`emit_account_created` Sends Plaintext Password Over IPC:**
- Symptoms: When the agent creates an ATS account during automation, it emits the plaintext password to the desktop app over JSONL stdout (`emit_account_created(..., password=password, password_provided=True)`). The desktop app then stores this directly.
- Files: `ghosthands/output/jsonl.py:238-266`, `ghosthands/cli.py:1485-1494`
- Trigger: Any Workday (or other platform) first-time account creation flow.
- Workaround: None currently. Mitigate by ensuring JSONL pipe is only read by the Desktop app process, not logged.

**Credential Generation Does Not Store the Generated Password:**
- Symptoms: When no stored credentials are found, `executor.py` generates an 18-char password on the fly and passes it to the agent. If the agent successfully creates an account, the generated password is NOT persisted back to `gh_user_credentials`. The user cannot log in to the ATS platform later without HITL/emit capturing the password.
- Files: `ghosthands/worker/executor.py:197-208`
- Trigger: First-time Workday application for a user with no stored credentials, run via the worker (not CLI) path.
- Workaround: The CLI path emits `account_created` events to the Desktop app which stores credentials. The worker path has no equivalent mechanism.

---

## Security Considerations

**`DomainLockdown.freeze()` Is Never Called:**
- Risk: `DomainLockdown` exposes `add_allowed_domain()` which can extend the allowlist at runtime. If not frozen after construction, a prompt-injection attack that convinces the LLM to call a hypothetical "allow domain" action could bypass navigation restrictions.
- Files: `ghosthands/security/domain_lockdown.py:249-257`
- Current mitigation: No public tool action exposes `add_allowed_domain()` to the LLM. The DomainLockdown is not used directly in `factory.py` — domain enforcement is delegated to `BrowserProfile(allowed_domains=...)` from browser-use.
- Recommendations: Call `lockdown.freeze()` immediately after construction in `create_job_agent`. Audit that browser-use's `BrowserProfile` actually enforces the domain list at the navigation-request level (not just as a hint).

**Auth Debug Logging is a Latent Secret Leakage Path:**
- Risk: `_log_auth_debug_credentials()` in `cli.py` logs plaintext credentials via `structlog.warning()`. If a log aggregator (Datadog, CloudWatch) is ever connected and `GH_DEBUG_AUTH_CREDENTIALS=1` is accidentally set, all user passwords would appear in the aggregator.
- Files: `ghosthands/cli.py:416-439`
- Current mitigation: Guard checks `GH_DEBUG_AUTH_CREDENTIALS != "1"`. Not set in production `.env.example`.
- Recommendations: Remove `password=` from the structlog call entirely; keep only `password_length=` and `has_password=True`. Or delete the function and replace with a pytest-only fixture.

**`DomainLockdown` Not Wired Into Navigation in Worker Path:**
- Risk: `ghosthands/security/domain_lockdown.py` and `ghosthands/browser/watchdogs/` define a full navigation interception system, but in the worker execution path (`factory.py`), domain enforcement is only applied via `BrowserProfile(allowed_domains=...)`. The `DomainLockdown` class itself (with stats tracking and `on_blocked` callback) is never instantiated in the worker flow.
- Files: `ghosthands/agent/factory.py:128-136`, `ghosthands/security/domain_lockdown.py`
- Current mitigation: `BrowserProfile.allowed_domains` does provide domain filtering.
- Recommendations: Instantiate `DomainLockdown` in `run_job_agent`, wire its `on_blocked` to structured logging, and pass its `get_allowed_domains()` to `BrowserProfile` for a consistent audit trail.

---

## Performance Bottlenecks

**`fill_executor.py` Evaluates Many Sequential `page.evaluate()` Calls:**
- Problem: The fill loop in `fill_executor.py` issues many sequential async JavaScript evaluations (one per field for read, one for write, additional ones for dropdowns). Each `page.evaluate()` is a round-trip to the Playwright browser process.
- Files: `ghosthands/dom/fill_executor.py` (throughout)
- Cause: Each control-type strategy is isolated and reads/writes the DOM independently. There is no batching of field writes.
- Improvement path: Batch multiple field writes into a single `page.evaluate()` call where field types permit it (text inputs, selects). The existing `_FILL_FIELD_JS` and `_SELECT_GROUPED_DATE_PICKER_VALUE_JS` scripts could be composed into a multi-field runner.

**`detect_blockers()` Runs Three Sequential `page.evaluate()` Calls Per Invocation:**
- Problem: Each call to `detect_blockers()` in `blocker_detector.py` issues up to three sequential evaluations: URL check, DOM selector scan, text body scan.
- Files: `ghosthands/security/blocker_detector.py:247-343`
- Cause: Early returns exist only for high-confidence URL hits. The DOM selector check passes the full selector list to JS for a single call, which is good, but the text body extraction is always a separate evaluate.
- Improvement path: Combine the selector scan and text extraction into a single `page.evaluate()` invocation. This was already done for DOM selectors — extend the JS to also return body text.

---

## Fragile Areas

**`domhand_fill.py` Profile Access via Global Environment Reads:**
- Files: `ghosthands/actions/domhand_fill.py:3017-3064`
- Why fragile: `_get_profile_data()` reads `GH_USER_PROFILE_PATH`, `GH_USER_PROFILE_TEXT`, or `GH_USER_PROFILE_JSON` from `os.environ` each time it's called. The profile is re-read from disk on every invocation (with no caching noted). A test that doesn't set these env vars will silently receive an empty profile.
- Safe modification: Always set `GH_USER_PROFILE_PATH` in test fixtures. Long-term: pass profile as a parameter, not via env.
- Test coverage: Unit tests in `tests/unit/test_domhand_fixes.py` mock the profile via the env var, but the pattern is fragile under parallel test runs.

**`StagehandLayer` Is a Process-Level Singleton:**
- Files: `ghosthands/stagehand/layer.py:377-395`
- Why fragile: `get_stagehand_layer()` returns a module-level singleton that holds a live Stagehand session. Tests that use `reset_stagehand_layer()` must do so carefully; any test failure that skips teardown leaves stale session state that can affect subsequent tests.
- Safe modification: Use `reset_stagehand_layer()` in pytest fixtures with `yield`-based teardown. Never call `get_stagehand_layer()` at module import time.
- Test coverage: `tests/unit/test_stagehand_layer.py` exists but relies on mocking the Stagehand SDK.

**`_SAME_TAB_GUARD_INSTALLED` Is a Module-Level Set Keyed by Session Object `id()`:**
- Files: `ghosthands/agent/hooks.py:37`
- Why fragile: `_SAME_TAB_GUARD_INSTALLED` stores `id(browser_session)` to avoid reinstalling the init script. Python's `id()` values can be reused after garbage collection. In a long-running worker processing many jobs, a new `BrowserSession` object could receive the same address as an old one, causing the guard to skip installation for the new session.
- Safe modification: Key on a stable session UUID rather than `id()`, or clear the set after each job completes in `run_job_agent`.
- Test coverage: None for this specific scenario.

**`HITLManager._poll_for_resume()` Uses `__import__("uuid")` Inline:**
- Files: `ghosthands/worker/hitl.py:241`
- Why fragile: `__import__("uuid")` inside a loop body is unusual and bypasses Python's module caching (though in practice CPython does cache it). It signals this code was written ad-hoc and may be missed in refactors.
- Safe modification: Add `import uuid` at the top of the file.
- Test coverage: `tests/ci/test_hitl_manager.py` mocks asyncpg, so this code path may never execute in tests.

---

## Scaling Limits

**Worker Is Single-Job Sequential:**
- Current capacity: One job per worker process at a time. The `run_worker()` poll loop claims one job, awaits its full completion, then polls again.
- Limit: Throughput is bounded by average job duration (~3-5 minutes). Ten concurrent jobs would require ten separate worker processes (10 browser instances, ~10 Playwright processes).
- Scaling path: Implement a multi-slot worker that runs N jobs in parallel via `asyncio.gather`, each with its own browser session and cost tracker. Requires eliminating the global profile env var and the `_SAME_TAB_GUARD_INSTALLED` keying issue first.

**In-Memory Runtime Learning Does Not Scale Across Workers:**
- Current capacity: Learned aliases and interaction recipes are per-process.
- Limit: Multi-worker deployments each maintain independent learning state. A recipe learned on worker-1 is invisible to worker-2.
- Scaling path: Persist learning data to a shared Redis key or Postgres table (`gh_runtime_learning`) and load at job start.

---

## Dependencies at Risk

**`browser_use/` Is a Vendored Fork Without Upstream Tracking:**
- Risk: The `browser_use/` directory is the upstream `browser-use` library vendored directly into the repo for patching. There is no explicit mechanism (submodule, patch file series) for tracking or applying upstream changes. As browser-use evolves (it is actively maintained), divergence will grow.
- Impact: Security fixes and new browser-use features will not be available without manual merge work. The fork is already multiple commits ahead of the public release.
- Migration plan: Consider git submodules with a patch layer, or fork on GitHub with a PR-based upstream sync process. Document which patches have been applied and why.

**`stagehand` Python SDK Is a Secondary Dependency With Unclear Stability:**
- Risk: The Stagehand Python SDK (`from stagehand import AsyncStagehand`) is used as an optional fallback layer. Its API stability, versioning policy, and maintenance status are unclear from within the codebase.
- Impact: Stagehand API changes would silently degrade fallback behavior; since Stagehand errors are caught and swallowed (`return ActResult(success=False, ...)`), breakage would appear as DomHand fallback failures.
- Migration plan: Pin the `stagehand` package version in `pyproject.toml`. Add an integration test that verifies the `AsyncStagehand` constructor signature.

---

## Test Coverage Gaps

**`ghosthands/security/blocker_detector.py` Has No Unit Tests:**
- What's not tested: URL pattern matching, DOM selector detection logic, text pattern matching, the non-blocking reCAPTCHA differentiation heuristic.
- Files: `ghosthands/security/blocker_detector.py`
- Risk: A regex change or confidence threshold adjustment could silently cause all jobs to report CAPTCHA blockers (false positive) or miss real ones (false negative).
- Priority: High — this is in the critical path for every job that encounters anti-bot measures.

**`ghosthands/worker/executor.py` Has No Test for Generated-Credential Path:**
- What's not tested: The code path at lines 197-208 that auto-generates a password when no stored credentials exist, and the downstream behavior when this generated credential is used.
- Files: `ghosthands/worker/executor.py:197-208`
- Risk: Silent regressions in password generation policy or credential routing to the agent.
- Priority: Medium — affects all first-time applications on platforms requiring login.

**`ghosthands/worker/poller.py` Has No Unit Tests:**
- What's not tested: Graceful shutdown signal handling, exponential backoff on consecutive poll errors, the `MAX_CONSECUTIVE_ERRORS` circuit breaker.
- Files: `ghosthands/worker/poller.py`
- Risk: Shutdown race conditions or backoff bugs could cause the worker to exit prematurely or spin-loop in error states.
- Priority: Medium.

**CI Dropdown Tests Have Multiple `@pytest.mark.skip` Entries:**
- What's not tested: ARIA menu detection, custom dropdown detection, several timeout-sensitive dropdown scenarios.
- Files: `tests/ci/interactions/test_dropdown_native.py:276,316,353,394,426,458,490`, `tests/ci/interactions/test_dropdown_aria_menus.py:150,186,225`
- Risk: Dropdown filling regressions on ARIA-menu-based ATS platforms (common in Workday custom widgets).
- Priority: Medium — these are the hardest-to-fill field types.

**`ghosthands/integrations/database.py` SQL Is Entirely Untested at the Unit Level:**
- What's not tested: `load_credentials()` domain-matching SQL, `poll_for_jobs()` priority ordering, `listen_for_signals()` NOTIFY subscription lifecycle.
- Files: `ghosthands/integrations/database.py`
- Risk: SQL logic bugs (e.g., wrong credential selected for a multi-domain user) would only be caught in production.
- Priority: Medium — use `asyncpg` test utilities with a real Postgres DB or `pytest-asyncio` with `asyncpg_test_db` fixture.

---

*Concerns audit: 2026-03-24*
