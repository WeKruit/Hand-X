# Architecture

**Analysis Date:** 2026-03-24

## Pattern Overview

**Overall:** Tiered DOM-first automation agent with LLM escalation

**Key Characteristics:**
- DOM manipulation is the primary fill strategy — LLM vision is the last resort
- Two distinct execution modes: server worker (polls VALET/Postgres) and desktop CLI (stdio JSONL IPC)
- Layered fill hierarchy: DomHand DOM-first → Stagehand semantic → browser-use generic vision
- Per-job budget and step-limit enforcement with hard budget exceptions
- Platform-aware guardrails injected into the agent system prompt per ATS type
- All I/O is async (asyncpg, httpx, Playwright)

## Layers

**Entry / Transport Layer:**
- Purpose: Accept job requests from two distinct sources
- Locations: `ghosthands/main.py` (worker mode), `ghosthands/cli.py` (desktop binary mode)
- Worker mode: polls `gh_automation_jobs` table via Postgres, receives job rows
- Desktop mode: receives `--job-url`, `--profile`, `--resume` via CLI args; communicates via JSONL on stdout + commands on stdin
- Depends on: Worker layer, Bridge layer
- Used by: External callers (VALET API, GH-Desktop-App Electron process)

**Worker / Orchestration Layer:**
- Purpose: Job lifecycle management — claim, execute, heartbeat, report, release
- Locations: `ghosthands/worker/poller.py`, `ghosthands/worker/executor.py`, `ghosthands/worker/hitl.py`, `ghosthands/worker/cost_tracker.py`
- `poller.py`: Main poll loop with SIGTERM/SIGINT shutdown, exponential backoff on errors, single-task-per-worker model
- `executor.py`: Orchestrates one job end-to-end: load resume → decrypt credentials → detect platform → validate domain → run agent → write DB → callback VALET
- `hitl.py`: Human-in-the-loop pause via Postgres LISTEN/NOTIFY; blocks on `gh_job_signal_{job_id}` channel up to 5 minutes
- `cost_tracker.py`: Per-step token tracking, budget enforcement by quality preset (`speed`/$0.05, `balanced`/$0.50, `quality`/$1.00)
- Depends on: Agent layer, Integration layer, Security layer
- Used by: Entry layer

**Agent Layer:**
- Purpose: browser-use Agent configuration and lifecycle
- Locations: `ghosthands/agent/factory.py`, `ghosthands/agent/hooks.py`, `ghosthands/agent/prompts.py`
- `factory.py`: Assembles `browser_use.Agent` with LLM, `BrowserProfile`, DomHand tools, system prompt, sensitive_data, cost tracking, and vision mode
- `hooks.py`: `on_step_start` / `on_step_end` callbacks for budget enforcement, blocker detection, HITL signal polling, and step tracing
- `prompts.py`: Builds the `extend_system_message` string injected after browser-use's own system prompt; includes per-platform guardrails and DomHand action priority ordering
- Depends on: DomHand Actions layer, Platform layer, LLM layer, Security layer
- Used by: Worker/Orchestration layer

**DomHand Actions Layer:**
- Purpose: Custom browser-use `@tools.action` definitions for DOM-first form filling
- Location: `ghosthands/actions/`
- Registration entry: `ghosthands/actions/__init__.py:register_domhand_actions(tools)` — registers all actions on the `Tools` controller
- Key actions:
  - `domhand_fill` — primary workhorse: extracts all fields, generates answers via single LLM call (Haiku/Gemini Flash), fills via Playwright DOM
  - `domhand_assess_state` — page state classifier (advanceable / review / confirmation / presubmit_single_page); must be called before advancing
  - `domhand_select` — complex custom dropdown handler (Workday portals, combobox widgets)
  - `domhand_interact_control` — targeted radio/checkbox/toggle/button-group recovery
  - `domhand_upload` — resume/cover letter file upload via file input
  - `domhand_close_popup` — modal/interstitial dismissal
  - `domhand_check_agreement` — agreement checkbox on auth pages
  - `stagehand_fill_field` / `stagehand_observe_fields` — Stagehand semantic layer escalation
- Depends on: DOM layer, Profile layer, Stagehand layer
- Used by: Agent layer (invoked by the LLM via tool calls)

**DOM Layer:**
- Purpose: Low-level DOM manipulation utilities consumed by DomHand actions
- Location: `ghosthands/dom/`
- Key modules:
  - `fill_executor.py` — per-control-type fill dispatcher (text, select, dropdown, radio, checkbox, date, button-group)
  - `fill_profile_resolver.py` — maps form fields to applicant profile values using semantic intent, QA matching, entry-data resolution
  - `fill_browser_scripts.py` — JavaScript snippets injected via `page.evaluate()` for all DOM operations
  - `fill_llm_answers.py` — single cheap LLM call (Haiku) to generate open-ended answers from profile + fields
  - `fill_verify.py` — post-fill verification logic
  - `dropdown_fill.py`, `dropdown_match.py`, `dropdown_verify.py` — dropdown-specific strategies
  - `field_extractor.py`, `label_resolver.py`, `option_discovery.py`, `shadow_helpers.py` — field discovery
- Depends on: Profile layer, LLM layer, browser-use `BrowserSession`
- Used by: DomHand Actions layer

**Profile Layer:**
- Purpose: Applicant profile normalization and canonical value resolution
- Locations: `ghosthands/profile/canonical.py`, `ghosthands/dom/fill_profile_resolver.py`, `ghosthands/bridge/profile_adapter.py`
- `canonical.py`: `CanonicalProfile` / `CanonicalValue` Pydantic models with provenance tracking (`explicit`, `derived`, `policy`)
- `fill_profile_resolver.py`: Maps field labels to canonical profile values using semantic intent classification and runtime-learned aliases
- `profile_adapter.py`: camelCase → snake_case conversion for profiles arriving from the Desktop bridge (TypeScript conventions)
- Depends on: `ghosthands/runtime_learning.py` for learned question aliases and interaction recipes
- Used by: DOM layer, DomHand Actions layer

**Platform Layer:**
- Purpose: ATS-specific guardrails, URL detection, and fill-strategy configuration
- Location: `ghosthands/platforms/`
- Registry: `ghosthands/platforms/__init__.py` — `detect_platform(url)` returns `workday|greenhouse|lever|smartrecruiters|generic`
- Platform configs (`PlatformConfig` Pydantic models) provide: URL patterns, content markers, allowed domains, form strategy, automation-id selectors, fill overrides
- Supported: `workday.py`, `greenhouse.py`, `lever.py`, `smartrecruiters.py`, `generic.py`
- Workday defines a 6-state auth state machine: `still_create_account → native_login → verification_required → authenticated_or_application_resumed → explicit_auth_error → unknown_pending`
- Depends on: Security layer (domain allowlists)
- Used by: Agent layer (system prompt injection), Worker/Orchestration layer (domain validation), Security layer

**Security Layer:**
- Purpose: Domain allowlisting and credential encryption
- Location: `ghosthands/security/`
- `domain_lockdown.py`: `DomainLockdown` class enforces per-session URL allowlists; per-platform allowlists + CDN whitelist; `check_and_record()` intercepts all navigations
- `blocker_detector.py`: Detects CAPTCHA, login walls, 2FA blockers that trigger HITL pause
- Credential decryption: `ghosthands/integrations/credentials.py` — AES-256-GCM via `GH_CREDENTIAL_ENCRYPTION_KEY`
- Depends on: Platform layer (domain lists)
- Used by: Worker/Orchestration layer, Agent layer

**Integration Layer:**
- Purpose: External system clients — VALET API, Postgres, resume loader
- Location: `ghosthands/integrations/`
- `database.py`: asyncpg raw SQL — `gh_automation_jobs` table; atomic job claim via `FOR UPDATE SKIP LOCKED`; heartbeat updates every 30s; LISTEN/NOTIFY for HITL signals
- `valet_callback.py`: `ValetClient` — httpx async client; fire-and-forget with 3 retries; `report_running()`, `report_progress()`, `report_completion()`; auth via `X-GH-Service-Key` header
- `credentials.py`: AES-256-GCM decryption of stored credentials
- `resume_loader.py`: Loads resume profile from Postgres by user_id
- Depends on: Config layer
- Used by: Worker/Orchestration layer

**LLM Layer:**
- Purpose: LLM client factory with VALET proxy routing
- Location: `ghosthands/llm/client.py`
- `get_chat_model()`: Returns LangChain-compatible chat model; routes through VALET proxy when `GH_LLM_PROXY_URL` is set (runtime grant token auth)
- Supports: Anthropic (Claude), Google (Gemini), OpenAI
- Default agent model: `gemini-3-flash-preview` (configurable via `GH_AGENT_MODEL`)
- Default DomHand answer model: `gemini-3-flash-preview` (configurable via `GH_DOMHAND_MODEL`)
- Depends on: Config layer
- Used by: Agent layer, DOM layer (LLM answer generation)

**Config Layer:**
- Purpose: Settings and model catalog
- Location: `ghosthands/config/`
- `settings.py`: Pydantic `BaseSettings` — all env vars prefixed `GH_`; `.env` auto-loaded; single `settings` singleton
- `models.py`: Model cost catalog for cost estimation per token

**Stagehand Layer:**
- Purpose: Thin adapter over Stagehand Python SDK for semantic form interactions
- Location: `ghosthands/stagehand/`
- `layer.py`: `StagehandLayer` singleton; `ensure_started(cdp_url)` lazily initializes; shares the browser-use browser session via CDP URL; never owns browser process
- `compat.py`, `sea_process_quiet.py`: Compatibility and subprocess management
- Depends on: browser-use browser session (CDP URL), LLM layer
- Used by: DomHand Actions layer (stagehand_fill_field, stagehand_observe_fields actions)

**Bridge Layer:**
- Purpose: Desktop app IPC protocol (Electron ↔ Hand-X process)
- Location: `ghosthands/bridge/`
- `protocol.py`: JSONL stdin command listener; `listen_for_cancel()`, `wait_for_review_command()`; serialized stdin reads with 64KB line guard
- `profile_adapter.py`: camelCase→snake_case profile conversion for Desktop-originated profiles
- Output: `ghosthands/output/jsonl.py` — `emit_event()` writes `ProgressEvent`-compatible JSONL to saved stdout fd; stdout redirected to stderr during init

**Runtime Learning Layer:**
- Purpose: Session-scoped semantic alias learning and interaction recipe caching
- Location: `ghosthands/runtime_learning.py`
- Caches: question label → `SemanticQuestionIntent` aliases learned during a run; interaction recipes (platform + host + label → successful strategy)
- 20 recognized semantic intents: work_authorization, visa_sponsorship, salary_expectation, gender, race_ethnicity, veteran_status, disability_status, etc.
- Depends on: nothing (pure in-memory)
- Used by: DOM/Profile layer

## Data Flow

**Server Worker Flow:**

1. `ghosthands/worker/poller.py:run_worker()` polls `gh_automation_jobs` via Postgres (`FOR UPDATE SKIP LOCKED`)
2. Claims job row, creates asyncio task for `executor.execute_job(job, db, valet)`
3. Executor: notifies VALET running → loads resume profile from DB → decrypts credentials → detects ATS platform from URL → validates domain against allowlist
4. Calls `ghosthands/agent/factory.py:run_job_agent()` which creates `browser_use.Agent` with DomHand tools registered
5. Agent runs: for each form page — calls `domhand_fill` (DOM extraction → single Haiku LLM call → Playwright fill) → calls `domhand_assess_state` → handles blockers with `domhand_select`/`domhand_interact_control` → escalates to Stagehand or generic browser-use if needed
6. Step hooks (`ghosthands/agent/hooks.py`) track cost per step; raise `BudgetExceededError` if over limit
7. If agent reports blocker: `HITLManager.pause_job()` sets status to `needs_human`, VALET notified; `wait_for_resume()` blocks on Postgres NOTIFY
8. Agent completes: result extracted from `AgentHistoryList`, written to DB, VALET completion callback fired

**Desktop CLI Flow:**

1. Electron spawns `hand-x` process with `--job-url --profile --resume --output-format jsonl`
2. `ghosthands/cli.py:main()` parses args, converts camelCase profile via `bridge/profile_adapter.py`
3. Installs stdout guard (JSONL on real stdout, logs on stderr)
4. Starts `bridge/protocol.py:listen_for_cancel()` listener for stdin commands
5. Calls `ghosthands/agent/factory.py:run_job_agent(keep_alive=True)` — browser stays open for human review
6. `ghosthands/output/jsonl.py:emit_event()` streams `ProgressEvent` objects to Electron

**DomHand Fill Flow (per page):**

1. `domhand_fill` called by agent as first action on any form page
2. Injects `__ff` helper library into page via `page.evaluate()`
3. Extracts all form fields (labels, types, options, required flags) from DOM
4. Sends fields + resume profile to `fill_llm_answers.py` for a single Haiku batch call
5. `fill_profile_resolver.py` matches each field to canonical profile value or LLM answer
6. `fill_executor.py` dispatches per-field fill via appropriate JS strategy (native setter, CDP, click)
7. Re-extracts to verify; handles newly revealed conditional fields; up to `MAX_FILL_ROUNDS` rounds
8. Returns `ActionResult` with filled/failed/unfilled counts

**State Management:**
- Per-job state in Postgres (`gh_automation_jobs` row): `pending → queued → running → completed/failed/needs_human/cancelled`
- Per-session runtime learning in `ghosthands/runtime_learning.py` (in-memory, not persisted)
- Cost state in `CostTracker` instance (in-memory per job execution)
- HITL state via Postgres LISTEN/NOTIFY on `gh_job_signal_{job_id}` channel

## Key Abstractions

**DomHandFillParams / FillFieldResult / FormField:**
- Purpose: Pydantic models for action parameters and DOM field representation
- Examples: `ghosthands/actions/views.py`
- Pattern: All action inputs/outputs are typed Pydantic models; `FormField` carries `field_id`, `field_type`, `options`, `widget_kind`, `component_field_ids`

**PlatformConfig:**
- Purpose: Per-ATS configuration bundle (URL patterns, domains, form strategy, selectors)
- Examples: `ghosthands/platforms/views.py`, instantiated in `workday.py`, `greenhouse.py`, `lever.py`, `smartrecruiters.py`, `generic.py`
- Pattern: Registry pattern — `_PLATFORM_REGISTRY` dict in `ghosthands/platforms/__init__.py`; looked up by `detect_platform(url)`

**CanonicalProfile:**
- Purpose: Normalized applicant profile with provenance metadata
- Examples: `ghosthands/profile/canonical.py`
- Pattern: `CanonicalValue(key, value, provenance, source_path)` tracks whether a value is `explicit` (from profile), `derived`, or `policy` (default answer)

**CostTracker:**
- Purpose: Per-job LLM cost accumulation and budget enforcement
- Examples: `ghosthands/worker/cost_tracker.py`
- Pattern: `track_step(step, tokens_in, tokens_out, model)` → raises `BudgetExceededError` or `StepLimitExceededError` when limits crossed

**StepHooks:**
- Purpose: browser-use `on_step_start`/`on_step_end` callbacks for budget, HITL, and tracing
- Examples: `ghosthands/agent/hooks.py`
- Pattern: Passed to `agent.run(on_step_start=hooks.on_step_start, on_step_end=hooks.on_step_end)`

**LearnedQuestionAlias / LearnedInteractionRecipe:**
- Purpose: Session-scoped semantic caching to avoid re-querying LLM for the same field labels
- Examples: `ghosthands/runtime_learning.py`
- Pattern: Module-level dicts keyed by normalized label; populated during `fill_profile_resolver` calls

## Entry Points

**Worker Entry (`ghosthands`):**
- Location: `ghosthands/main.py:main()`
- Triggers: `ghosthands` CLI command (registered in `pyproject.toml`); `python -m ghosthands.main`
- Responsibilities: `asyncio.run(run_worker())` — starts the Postgres poll loop

**Desktop Binary Entry (`hand-x`):**
- Location: `ghosthands/cli.py:main()`
- Triggers: `hand-x` CLI command; spawned by GH-Desktop-App Electron process
- Responsibilities: Parse CLI args, install stdout JSONL guard, convert profile, run agent with `keep_alive=True`, emit JSONL progress events

**Module Direct Entry:**
- Location: `ghosthands/__main__.py`
- Triggers: `python -m ghosthands`

## Error Handling

**Strategy:** Structured exception types with explicit error codes; never crash the poll loop

**Patterns:**
- `BudgetExceededError` / `StepLimitExceededError` — raised in `CostTracker.track_step()`; caught in `executor.execute_job()` → job marked `failed` with error code, VALET notified
- `asyncio.CancelledError` — re-raised after marking job `cancelled`; allows graceful shutdown
- General `Exception` in `poller._execute_with_error_handling()` — catches all executor errors, marks job `failed`, fires VALET failsafe callback
- Consecutive error counter in `poller.run_worker()` — breaks loop and exits after 10 consecutive poll errors
- All integration callbacks (VALET, heartbeat) are fire-and-forget — failures logged but never fail the job
- Domain blocked returns structured `{"success": False, "error_code": "domain_blocked"}` immediately without agent launch

## Cross-Cutting Concerns

**Logging:** structlog throughout; structured key-value pairs; `logger.bind(job_id=..., worker_id=...)` for contextual fields; all logs to stderr in desktop mode

**Validation:** Pydantic v2 models for all action parameters, settings, profile data, and platform configs

**Authentication:**
- VALET callbacks: `X-GH-Service-Key` HMAC header
- LLM proxy: Bearer runtime grant token (`GH_LLM_RUNTIME_GRANT`)
- ATS credentials: AES-256-GCM encrypted at rest; decrypted in-memory only; passed to browser-use as `sensitive_data` (never appear in prompts)
- Profile PII: Written to chmod-0600 temp file; env var `GH_USER_PROFILE_PATH` set; temp file deleted after agent run

---

*Architecture analysis: 2026-03-24*
