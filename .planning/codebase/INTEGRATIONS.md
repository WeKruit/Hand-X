# External Integrations

**Analysis Date:** 2026-03-24

## APIs & External Services

**LLM Providers (primary):**
- Anthropic — agent decisions and DomHand answer generation
  - SDK/Client: `anthropic>=0.49` via `ghosthands/llm/client.py` (`get_anthropic_client()`)
  - Auth: `ANTHROPIC_API_KEY` / `GH_ANTHROPIC_API_KEY` env var
  - Default models: `claude-sonnet-4-20250514` (agent), `claude-haiku-4-5-20251001` (DomHand)

- Google Gemini — default agent model (cheaper than Anthropic)
  - SDK/Client: `google-genai>=1.0` via `browser_use/llm/google/chat.py` (`ChatGoogle`)
  - Auth: `GOOGLE_API_KEY` / `GH_GOOGLE_API_KEY` env var
  - Default model: `gemini-3-flash-preview` (both `GH_AGENT_MODEL` and `GH_DOMHAND_MODEL`)

- OpenAI — optional fallback provider
  - SDK/Client: `openai>=1.50` via `browser_use/llm/openai/chat.py` (`ChatOpenAI`)
  - Auth: `OPENAI_API_KEY` / `GH_OPENAI_API_KEY` env var
  - Models: `gpt-4o`, `gpt-4o-mini` in model catalog (`ghosthands/config/models.py`)

**LLM Proxy (VALET managed inference):**
- VALET LLM Proxy — routes LLM calls through VALET's managed inference endpoint
  - Client: custom routing in `ghosthands/llm/client.py` (`get_chat_model()`)
  - Auth: `GH_LLM_RUNTIME_GRANT` (lease-scoped runtime grant token); sent as `api_key` or `x-goog-api-key`
  - Config: `GH_LLM_PROXY_URL` — e.g., `https://api.valet.wekruit.com/api/v1/local-workers`
  - Gemini: proxy URL + `/gemini` suffix; Anthropic: proxy URL directly; OpenAI: overridden to Claude Sonnet

**Stagehand (optional AI browser layer):**
- Stagehand Python SDK — semantic form fill and page observation via AI vision
  - Client: `ghosthands/stagehand/layer.py` (`StagehandLayer` singleton)
  - Auth: `BROWSERBASE_API_KEY` (required for Stagehand sessions)
  - CDP connection: shares browser process with browser-use via CDP WebSocket URL
  - Models: `anthropic/claude-haiku-4-5-20251001` (default), `google/gemini-3-flash-preview`, `openai/gpt-5.4-mini`
  - Used as fallback when DomHand DOM-first fill fails

## Data Storage

**Databases:**
- PostgreSQL — primary data store for job queue, credentials, resume profiles, HITL signals
  - Connection: `GH_DATABASE_URL` env var (asyncpg DSN format)
  - Client: `asyncpg>=0.30` — raw SQL, no ORM; connection pool (`min=2, max=10`)
  - Implementation: `ghosthands/integrations/database.py` (`Database` class)
  - Tables used:
    - `gh_automation_jobs` — job queue (polling, claiming, status updates, heartbeats)
    - `gh_user_credentials` — encrypted ATS credentials (GH envelope format)
    - `gh_job_events` — audit/timeline event log
    - `platform_credentials` — VALET-format credentials (scrypt-derived AES key)
    - `resumes` — parsed resume JSONB data (VALET table, no `gh_` prefix)

**File Storage:**
- Local filesystem only for resume files passed via `--resume` CLI arg (PDF/DOCX)
- No object storage integration detected

**Caching:**
- Redis (optional) — step trace streaming for agent replay/debugging
  - Client: `redis>=5.2` (`redis.asyncio` in `ghosthands/step_trace.py`)
  - Config: `GH_STEP_TRACE_REDIS_URL`, `GH_STEP_TRACE_ENABLED` (default: false)
  - Usage: `XADD` to per-job stream `gh:job:{job_id}:steps` with TTL and maxlen

## Authentication & Identity

**ATS Credential Encryption:**
- AES-256-GCM (two formats) — implemented in `ghosthands/integrations/credentials.py`
  - GH envelope format: `base64(version:1 + keyId:2 + iv:12 + authTag:16 + ciphertext)` — key from `GH_CREDENTIAL_ENCRYPTION_KEY` (64 hex chars)
  - VALET format: `base64(iv:16 + authTag:16 + ciphertext)` — key derived via `scrypt(password, 'valet-cred-salt', 32)`
  - Rotation support: GH format includes `keyId` field for multi-key rotation

**VALET API Auth:**
- Shared secret HMAC — `X-GH-Service-Key` header in `ghosthands/integrations/valet_callback.py`
  - Config: `GH_VALET_CALLBACK_SECRET`

**Desktop Bridge Auth:**
- Runtime grant token — lease-scoped JWT passed via `--runtime-grant` CLI arg or `GH_LLM_RUNTIME_GRANT`
  - Used as both LLM proxy auth and desktop session identifier

## Monitoring & Observability

**Error Tracking:**
- Not detected — no Sentry, Rollbar, or equivalent integration

**Logs:**
- `structlog>=24.4` — structured key-value logging throughout
- All app code uses `structlog.get_logger()`; no `print()` statements in production code
- In JSONL mode (desktop binary), `structlog` output goes to stderr; stdout reserved for JSONL events
- Log format: key-value pairs with event name as first arg (e.g., `logger.info("worker.job_claimed", job_id=..., ...)`)

**Cost Tracking:**
- Per-job LLM cost tracking — `ghosthands/worker/cost_tracker.py` (`CostTracker`)
  - Model pricing catalog: `ghosthands/config/models.py` (`MODEL_CATALOG`)
  - Budget limits: `GH_MAX_BUDGET_PER_JOB` (default `$0.50`); per-job-type overrides (Workday: `$2.00`)
  - Budget enforcement: `BudgetExceededError` / `StepLimitExceededError` raised in step hooks

## CI/CD & Deployment

**Hosting:**
- Docker — `Dockerfile` based on `python:3.12-slim`; system Chromium installed via apt
- GitHub Releases — standalone PyInstaller binaries uploaded as release assets

**CI Pipeline:**
- GitHub Actions — workflows in `.github/workflows/`
  - `test.yaml` — unit test suite (`pytest tests/unit/`)
  - `lint.yml` — `ruff check` + `ruff format --check`
  - `build-binary.yml` — PyInstaller binary build (macOS ARM primary; Linux/Windows in matrix but disabled)
  - `docker.yml` — Docker image build and publish
  - `publish.yml` — PyPI publish on version tags
  - `security-scan.yml` — security scanning
  - `notify-desktop.yml` — triggers `repository_dispatch` to `WeKruit/GH-Desktop-App` after release

## Webhooks & Callbacks

**Incoming:**
- Postgres LISTEN/NOTIFY — HITL signals for paused jobs
  - Channel: `gh_job_signal_{job_id_with_underscores}`
  - Payload: `{"action": "resume"|"cancel", "data": {...}}`
  - VALET sends NOTIFY when user clicks "Resume"/"Cancel" in UI
  - Implementation: `ghosthands/integrations/database.py` (`listen_for_signals()`)

**Outgoing:**
- VALET API callbacks — job status, progress, completion, needs_human
  - Endpoint: `{GH_VALET_API_URL}/api/v1/tasks/{job_id}/callback`
  - Auth: `X-GH-Service-Key` header
  - Client: `ghosthands/integrations/valet_callback.py` (`ValetClient`)
  - Methods: `report_status()`, `report_running()`, `report_progress()`, `report_completion()`, `report_needs_human()`, `report_resumed()`
  - Retry: 3 attempts with delays `[1.0, 3.0, 10.0]` seconds; never raises (fire-and-forget)
- GitHub `repository_dispatch` — notifies `WeKruit/GH-Desktop-App` after binary release
  - Event type: `hand-x-updated`
  - Payload: `{"version": "X.Y.Z"}`
  - Secret: `DESKTOP_REPO_PAT` GitHub Actions secret

## ATS Platform Support

**Supported platforms (domain allowlists in `ghosthands/security/domain_lockdown.py`):**
- Workday — `myworkdayjobs.com`, `myworkday.com`, `wd1–wd5.myworkdayjobs.com/myworkday.com`
- Greenhouse — `greenhouse.io`, `boards.greenhouse.io`, `job-boards.greenhouse.io`
- Lever — `lever.co`, `jobs.lever.co`
- SmartRecruiters — `smartrecruiters.com`, `jobs.smartrecruiters.com`
- Ashby — `ashbyhq.com`, `jobs.ashbyhq.com`
- iCIMS — `icims.com`, `careers-page.icims.com`
- Amazon — `amazon.jobs`, `amazon.com`
- LinkedIn — `linkedin.com` (allowlisted but not a primary application target)
- Generic — all of the above combined

**Platform-specific guardrail configs:** `ghosthands/platforms/workday.py`, `greenhouse.py`, `lever.py`, `smartrecruiters.py`, `generic.py`

## Environment Configuration

**Required env vars:**
- `GH_DATABASE_URL` — Postgres connection string (asyncpg format)
- `GH_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` — for DomHand answer generation
- `GH_GOOGLE_API_KEY` or `GOOGLE_API_KEY` — if using Gemini as agent model (default)
- `GH_CREDENTIAL_ENCRYPTION_KEY` — 64 hex chars for AES-256-GCM

**Optional env vars:**
- `GH_VALET_API_URL` — VALET callback base URL
- `GH_VALET_CALLBACK_SECRET` — HMAC shared secret for VALET callbacks
- `GH_LLM_PROXY_URL` — VALET managed inference proxy URL
- `GH_LLM_RUNTIME_GRANT` — lease-scoped grant token for proxy auth
- `GH_STEP_TRACE_REDIS_URL` — Redis for step tracing (disabled by default)
- `GH_STEP_TRACE_ENABLED` — enable Redis step tracing (default: `false`)
- `GH_HEADLESS` — run browser headless (default: `true`)
- `GH_WORKER_ID` — worker identity (default: `hand-x-1`)
- `GH_MAX_BUDGET_PER_JOB` — LLM cost cap per job (default: `0.50`)
- `GH_CDP_URL` — CDP URL of an existing browser (desktop-owned browser mode)

**Secrets location:**
- All secrets managed via ATM/Infisical (never run `fly secrets set` directly)
- Local development: `.env` file (auto-loaded by pydantic-settings)

---

*Integration audit: 2026-03-24*
