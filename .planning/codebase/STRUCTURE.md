# Codebase Structure

**Analysis Date:** 2026-03-24

## Directory Layout

```
Hand-X/
├── ghosthands/              # Main application package
│   ├── main.py              # Worker entry point (asyncio + poller)
│   ├── cli.py               # Desktop binary entry (JSONL IPC mode)
│   ├── __main__.py          # python -m ghosthands support
│   ├── __init__.py
│   ├── cost_summary.py      # Cost aggregation helpers
│   ├── env_bootstrap.py     # Early env setup
│   ├── runtime_learning.py  # Session-scoped semantic alias cache
│   ├── step_trace.py        # Redis Streams step tracing
│   ├── actions/             # browser-use @tools.action definitions (DomHand)
│   ├── agent/               # browser-use Agent factory, hooks, prompts
│   ├── bridge/              # Desktop app IPC protocol
│   ├── browser/             # Browser session watchdogs (currently stub)
│   ├── config/              # Pydantic settings + model cost catalog
│   ├── dom/                 # DOM manipulation utilities consumed by actions
│   ├── integrations/        # Postgres, VALET API, resume loader
│   ├── llm/                 # LLM client factory (proxy routing)
│   ├── output/              # JSONL event emitter for desktop IPC
│   ├── platforms/           # ATS-specific guardrails and URL detection
│   ├── profile/             # Canonical applicant profile models
│   ├── security/            # Domain lockdown + blocker detection
│   ├── stagehand/           # Stagehand semantic layer adapter
│   ├── visuals/             # Visual cursor patch for browser-use
│   └── worker/              # Job poller, executor, HITL, cost tracker
├── browser_use/             # Vendored browser-use library (forked in-tree)
├── tests/
│   ├── unit/                # Fast tests, no browser/DB
│   │   └── dom/             # DOM layer unit tests
│   ├── ci/                  # CI tests (browser, infrastructure, interactions, security)
│   │   ├── browser/
│   │   ├── infrastructure/
│   │   ├── interactions/
│   │   ├── models/
│   │   └── security/
│   ├── integration/         # Tests with browser/DB fixtures
│   ├── fixtures/            # Shared test fixtures
│   └── agent_tasks/         # Agent task definition files
├── examples/                # Example scripts and toy apps
│   └── toy-job-app/         # Local test job application form
├── docker/                  # Docker build files
│   └── base-images/         # Layered base images (chromium, python-deps, system)
├── scripts/                 # Utility scripts
├── skills/                  # Skill definitions (browser-use, remote-browser)
├── bin/                     # Binary outputs
├── dist/                    # Built distributions
├── build/                   # PyInstaller build artifacts
├── static/                  # Static assets
├── pyproject.toml           # Package config, deps, ruff, pytest settings
├── uv.lock                  # Lockfile (uv)
├── Dockerfile               # Standard Docker image
├── Dockerfile.fast          # Fast-build Docker image
└── apply.sh                 # Quick apply helper script
```

## Directory Purposes

**`ghosthands/actions/`:**
- Purpose: All browser-use `@tools.action` definitions visible to the LLM agent
- Contains: One file per action (prefix `domhand_`), `stagehand_tools.py`, `views.py` (Pydantic param/result models), `_highlight.py`, `combobox_toggle.py`
- Key files: `__init__.py` (registration entry), `domhand_fill.py` (primary workhorse), `domhand_assess_state.py`, `domhand_select.py`, `domhand_interact_control.py`
- Naming: `domhand_{verb}.py` for DomHand actions; `stagehand_tools.py` for Stagehand escalation tools

**`ghosthands/agent/`:**
- Purpose: browser-use Agent assembly and step lifecycle management
- Contains: `factory.py` (create + run agent), `hooks.py` (step callbacks), `prompts.py` (system prompt builder)
- Key files: `factory.py:create_job_agent()`, `factory.py:run_job_agent()`, `hooks.py:StepHooks`

**`ghosthands/bridge/`:**
- Purpose: Desktop app ↔ engine stdin/stdout JSONL protocol
- Contains: `protocol.py` (stdin command reader), `profile_adapter.py` (camelCase→snake_case profile conversion)

**`ghosthands/config/`:**
- Purpose: Application configuration and model cost catalog
- Contains: `settings.py` (Pydantic `BaseSettings` singleton), `models.py` (model catalog + cost estimation)
- All env vars use `GH_` prefix; `.env` auto-loaded

**`ghosthands/dom/`:**
- Purpose: Low-level DOM manipulation utilities; not directly exposed to agent
- Contains: `fill_executor.py` (per-type fill dispatcher), `fill_profile_resolver.py` (profile → field mapping), `fill_browser_scripts.py` (all injected JS), `fill_llm_answers.py` (single LLM batch call), `fill_verify.py`, `dropdown_fill.py`, `dropdown_match.py`, `dropdown_verify.py`, `field_extractor.py`, `label_resolver.py`, `option_discovery.py`, `shadow_helpers.py`, `validation_reader.py`, `views.py`

**`ghosthands/integrations/`:**
- Purpose: External system I/O — Postgres, VALET API, credentials, resume
- Contains: `database.py` (asyncpg raw SQL), `valet_callback.py` (httpx client), `credentials.py` (AES-256-GCM decryption), `resume_loader.py`

**`ghosthands/llm/`:**
- Purpose: LLM client factory with VALET proxy support
- Contains: `client.py:get_chat_model()`, `get_anthropic_client()`

**`ghosthands/output/`:**
- Purpose: JSONL event emission for desktop IPC
- Contains: `jsonl.py:emit_event()`, `jsonl.py:install_stdout_guard()`, `field_events.py`

**`ghosthands/platforms/`:**
- Purpose: ATS-specific configuration — URL detection, domain allowlists, form strategies
- Contains: `__init__.py` (registry + `detect_platform()`), `workday.py`, `greenhouse.py`, `lever.py`, `smartrecruiters.py`, `generic.py`, `views.py` (`PlatformConfig` model)
- Supported ATSs: Workday, Greenhouse, Lever, SmartRecruiters + generic fallback

**`ghosthands/profile/`:**
- Purpose: Canonical applicant profile normalization
- Contains: `canonical.py` (`CanonicalProfile`, `CanonicalValue` Pydantic models with provenance)

**`ghosthands/security/`:**
- Purpose: Navigation safety and blocker detection
- Contains: `domain_lockdown.py` (`DomainLockdown` class, `PLATFORM_ALLOWLISTS`), `blocker_detector.py`

**`ghosthands/stagehand/`:**
- Purpose: Stagehand Python SDK adapter (semantic layer for stubborn fields)
- Contains: `layer.py` (`StagehandLayer` singleton), `compat.py`, `sea_process_quiet.py`

**`ghosthands/worker/`:**
- Purpose: Job lifecycle — polling, execution, HITL, cost tracking
- Contains: `poller.py` (main poll loop), `executor.py` (single-job orchestrator), `hitl.py` (`HITLManager`), `cost_tracker.py` (`CostTracker`, `BudgetExceededError`, `StepLimitExceededError`)

**`browser_use/`:**
- Purpose: Vendored upstream browser-use library, kept in-tree for direct patching
- Generated: No — checked in
- Committed: Yes — intentionally vendored for in-tree modifications
- Do not add new Hand-X logic here; only apply upstream patches

**`tests/unit/`:**
- Purpose: Fast unit tests; no browser or DB required
- Subdirectory `dom/` contains DOM layer tests
- Run with: `pytest tests/unit/`

**`tests/ci/`:**
- Purpose: CI-level tests organized by concern
- Subdirectories: `browser/` (Playwright interactions), `infrastructure/` (DB, worker), `interactions/` (form filling), `models/` (LLM model tests), `security/` (domain lockdown, etc.)

**`tests/integration/`:**
- Purpose: Integration tests requiring live browser or DB fixtures

**`examples/toy-job-app/`:**
- Purpose: Local HTML job application form for integration testing without hitting real ATS
- Used by: CI interaction tests and local development

**`docker/base-images/`:**
- Purpose: Layered Docker base images for reproducible CI builds
- Layers: `system/` (OS deps), `python-deps/` (pip packages), `chromium/` (browser)

## Key File Locations

**Entry Points:**
- `ghosthands/main.py`: Worker mode entry — `asyncio.run(run_worker())`
- `ghosthands/cli.py`: Desktop binary entry — JSONL IPC mode
- `ghosthands/__main__.py`: `python -m ghosthands` support

**Configuration:**
- `ghosthands/config/settings.py`: All `GH_*` env vars; `settings` singleton used everywhere
- `ghosthands/config/models.py`: LLM model cost catalog for `CostTracker`
- `pyproject.toml`: Package deps, ruff config, pytest config, entry point scripts

**Core Automation Logic:**
- `ghosthands/worker/executor.py`: End-to-end job orchestration (load → detect → validate → run → report)
- `ghosthands/agent/factory.py`: Agent assembly and `run_job_agent()` convenience wrapper
- `ghosthands/actions/__init__.py`: DomHand action registration; add new actions here
- `ghosthands/actions/domhand_fill.py`: Primary DOM-first fill action
- `ghosthands/dom/fill_executor.py`: Per-control-type fill dispatcher

**Platform Detection:**
- `ghosthands/platforms/__init__.py`: `detect_platform(url)` — URL pattern matching + hosted Greenhouse detection
- `ghosthands/security/domain_lockdown.py`: `PLATFORM_ALLOWLISTS` — per-ATS navigation allowlists

**Testing:**
- `tests/unit/dom/`: DOM layer unit tests
- `tests/ci/interactions/`: Form filling interaction tests
- `tests/ci/security/`: Domain lockdown and security tests
- `tests/fixtures/`: Shared fixtures

## Naming Conventions

**Files:**
- `domhand_{verb}.py` — DomHand action files in `ghosthands/actions/`
- `fill_{concern}.py` — DOM fill utilities in `ghosthands/dom/` (e.g., `fill_executor.py`, `fill_profile_resolver.py`)
- `_{name}.py` (leading underscore) — Internal utilities not meant for direct import (e.g., `ghosthands/actions/_highlight.py`)
- `views.py` — Pydantic models for a module's I/O types (present in `actions/`, `dom/`, `platforms/`)
- `test_{module}.py` — Test files mirror source module name

**Directories:**
- Lowercase, snake_case: `dom/`, `fill_executor`, `cost_tracker`
- One directory per concern, not per layer

**Functions:**
- snake_case throughout
- Async functions: no special prefix; use `async def`
- Private helpers: leading underscore (e.g., `_fill_single_field()`, `_run_agent()`)

**Classes:**
- PascalCase: `CostTracker`, `HITLManager`, `DomainLockdown`, `CanonicalProfile`, `FormField`
- Pydantic models: end in descriptive noun (`Params`, `Result`, `Config`, `Profile`, `Value`)

**Environment Variables:**
- All prefixed `GH_`: `GH_DATABASE_URL`, `GH_AGENT_MODEL`, `GH_HEADLESS`, `GH_MAX_BUDGET_PER_JOB`

## Where to Add New Code

**New DomHand action:**
1. Create `ghosthands/actions/domhand_{verb}.py` with the async function
2. Add Pydantic param model to `ghosthands/actions/views.py`
3. Register in `ghosthands/actions/__init__.py:register_domhand_actions()` via `_register_action()`
4. Add unit test in `tests/unit/` or interaction test in `tests/ci/interactions/`

**New ATS platform:**
1. Create `ghosthands/platforms/{platform_name}.py` with a `PlatformConfig` instance
2. Register in `ghosthands/platforms/__init__.py` — add to `_PLATFORM_REGISTRY` and `_URL_PATTERNS`
3. Add domain allowlist entry to `ghosthands/security/domain_lockdown.py:PLATFORM_ALLOWLISTS`
4. Update `ghosthands/config/settings.py:allowed_domains` default list

**New worker integration (external service):**
- Implementation: `ghosthands/integrations/{service_name}.py`
- Client instantiation in: `ghosthands/worker/poller.py` (if needed per job cycle)

**New config setting:**
- Add field to `ghosthands/config/settings.py:Settings` class with `GH_` prefix convention
- Document in `.env.example`

**Shared DOM utilities:**
- Location: `ghosthands/dom/` — new module or addition to `fill_browser_scripts.py` for JS
- Consumed by: `ghosthands/actions/` and `ghosthands/dom/fill_executor.py`

**Utilities and cross-cutting:**
- Module-level helpers used across `ghosthands/`: place in `ghosthands/` root (e.g., `runtime_learning.py`, `step_trace.py`, `cost_summary.py`)

**Tests:**
- Unit (no browser/DB): `tests/unit/` — mirror source path (e.g., `tests/unit/dom/test_fill_executor.py`)
- CI interaction tests: `tests/ci/interactions/`
- Security tests: `tests/ci/security/`
- Integration (live browser): `tests/integration/`

## Special Directories

**`browser_use/`:**
- Purpose: Vendored upstream browser-use library for direct patching
- Generated: No
- Committed: Yes

**`.planning/`:**
- Purpose: GSD planning documents — codebase maps, phase plans
- Generated: Yes (by GSD tooling)
- Committed: Depends on team practice

**`build/` and `dist/`:**
- Purpose: PyInstaller and hatchling build outputs
- Generated: Yes
- Committed: No (typically gitignored)

**`examples/toy-job-app/`:**
- Purpose: Local test job application HTML form for CI and dev
- Generated: No
- Committed: Yes

---

*Structure analysis: 2026-03-24*
