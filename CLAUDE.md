# Hand-X (GhostHands v2)

Job application automation agent built on [browser-use](https://github.com/browser-use/browser-use). Takes a job URL + user credentials/resume, navigates the ATS, fills out the application, and submits it.

## Architecture

```
VALET API (assigns job) --> Hand-X Worker (polls for jobs)
                                |
                                v
                         browser-use Agent Loop
                           |              |
                      DomHand Actions   Generic browser-use
                      (DOM-first fill)  (fallback for complex UI)
                           |
                      Platform Guardrails
                      (Workday, Greenhouse, Lever, etc.)
```

**Flow per job:**
1. Worker polls VALET for pending jobs
2. Receives job payload (URL, encrypted credentials, resume, user answers)
3. Decrypts credentials via AES-256-GCM
4. Validates URL against allowed ATS domains
5. Launches browser-use agent with DomHand actions registered
6. Agent navigates to job URL, fills application, submits
7. Reports result back to VALET via callback

## How DomHand Works

DomHand is the key differentiator from raw browser-use. For every form field:

1. **DOM-first fill gate:** Query the DOM for input fields, extract labels/placeholders, match against user profile data. Use Playwright `page.fill()` / `page.select_option()` directly — no LLM call needed for straightforward fields.
2. **LLM answer generation:** For open-ended questions (e.g., "Why do you want to work here?"), call Haiku to generate a contextual answer from the user's resume + job description.
3. **browser-use generic fallback:** If DOM manipulation fails (shadow DOM, custom widgets, iframes), let browser-use handle it with its vision + action model.

This tiered approach keeps costs low (most fields filled without LLM) while handling edge cases via browser-use.

## Directory Structure

```
ghosthands/                 # GhostHands v2 application code
  main.py                   # Entry point — starts worker
  config/
    settings.py             # Pydantic BaseSettings (all GH_ env vars)
    models.py               # LLM model catalog + cost estimation
  agent/                    # browser-use agent loop orchestration
  browser/                  # Hand-X adapter layer over vendored browser_use
  actions/                  # @tools.action definitions for browser-use
  dom/                      # DomHand: DOM-first form filling logic
  platforms/                # ATS-specific guardrails (Workday, Greenhouse, Lever)
  worker/                   # Job polling, execution, lifecycle
  integrations/             # VALET API callbacks, external clients
  security/                 # Credential encryption, domain allowlisting
browser_use/                # Upstream browser-use library, kept vendored-clean
tests/
  unit/                     # Fast tests, no browser/DB
  integration/              # Tests with browser/DB fixtures
scripts/                    # Utility scripts
```

## Development Setup

```bash
# Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create venv and install deps
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# Install Playwright browsers
playwright install chromium

# Copy env template
cp .env.example .env
# Fill in GH_DATABASE_URL and API keys
```

## Environment Variables

All GhostHands vars are prefixed with `GH_`. See `.env.example` for the full list.

| Variable | Required | Description |
|----------|----------|-------------|
| `GH_DATABASE_URL` | Yes | Postgres connection string |
| `GH_ANTHROPIC_API_KEY` | Yes | For Haiku answer generation |
| `GH_WORKER_ID` | No | Worker identity (default: `hand-x-1`) |
| `GH_HEADLESS` | No | Run browser headless (default: `true`) |
| `GH_MAX_BUDGET_PER_JOB` | No | Max LLM $ per job (default: `0.50`) |
| `GH_VALET_API_URL` | No | VALET API base URL for callbacks |
| `GH_CREDENTIAL_ENCRYPTION_KEY` | No | 64 hex chars for AES-256-GCM |

## Running

```bash
# Start the worker
ghosthands

# Or directly
python -m ghosthands.main
```

## Testing

```bash
# All tests
pytest

# Unit only
pytest tests/unit/

# With verbose output
pytest -v
```

## Linting

```bash
ruff check .
ruff format .
```

## Key Conventions

- **Python 3.12+** — use modern syntax (`str | None`, `match` statements, `list[str]`)
- **asyncio everywhere** — all I/O is async (asyncpg, httpx, playwright)
- **Pydantic for data** — models, settings, validation
- **structlog for logging** — structured key-value logging, no print statements
- **browser-use `@tools.action` pattern** — custom actions registered on the agent's tool controller
- **Cost tracking** — every LLM call tracked against per-job budget via `config/models.py`
- **Security first** — credentials always encrypted at rest, domain allowlisting enforced before navigation
- **browser-use vendored clean** — keep `browser_use/` aligned with `upstream/stable`; Hand-X runtime behavior belongs in `ghosthands/agent/handx_agent.py`, `ghosthands/browser/`, and `ghosthands/browser/watchdogs/`

## Vendor Boundary

- `browser_use/` is vendored upstream code, not the place for Hand-X policy.
- If Hand-X needs custom agent behavior, browser/session behavior, tool behavior, or watchdog behavior, implement it in the GhostHands adapter layer:
  - `ghosthands.agent.handx_agent`
  - `ghosthands.browser.HandXBrowserProfile`
  - `ghosthands.browser.HandXBrowserSession`
  - `ghosthands.browser.HandXTools`
  - `ghosthands.browser.watchdogs.handx_*`
- Upstream sync rule: prefer restoring or fetching vendor files rather than patching them. New Hand-X behavior should be appended via wrappers/subclasses, not by modifying vendored `browser_use` files.

## Git

- Branch from `main`, PR back to `main`
- Commit format: `feat|fix|refactor(module): description`
- Run `ruff check . && ruff format --check .` before committing
