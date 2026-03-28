# Technology Stack

**Analysis Date:** 2026-03-24

## Languages

**Primary:**
- Python 3.12+ — All application code in `ghosthands/` and `browser_use/`

**Secondary:**
- JavaScript — DOM manipulation scripts bundled inside `browser_use/dom/**/*.js`
- Markdown — System prompt files in `browser_use/agent/system_prompts/*.md`

## Runtime

**Environment:**
- CPython 3.12 (minimum, enforced by `requires-python = ">=3.12"` in `pyproject.toml`)
- asyncio — entire codebase is async-first; all I/O uses `asyncio`, `asyncpg`, `httpx`

**Package Manager:**
- `uv` (Astral) — lockfile `uv.lock` present; install via `uv venv && uv pip install -e ".[dev]"`
- Lockfile: `uv.lock` (committed)

## Frameworks

**Core:**
- `browser-use` (vendored in-tree at `browser_use/`) — AI browser agent loop; provides `Agent`, `BrowserProfile`, `Tools` and all LLM chat adapters
- `playwright>=1.49` — browser automation underlying browser-use; Chromium driver
- `pydantic>=2.10` — data models, settings validation, action parameter models throughout `ghosthands/`
- `pydantic-settings>=2.7` — environment variable loading via `ghosthands/config/settings.py`

**LLM Abstraction:**
- `anthropic>=0.49` — Anthropic SDK used directly in `ghosthands/llm/client.py`; also via `browser_use/llm/anthropic/chat.py`
- `openai>=1.50` — OpenAI SDK via `browser_use/llm/openai/chat.py`
- `google-genai>=1.0` — Google Gemini via `browser_use/llm/google/chat.py`

**Testing:**
- `pytest>=8.3` — test runner; config in `pyproject.toml` (`asyncio_mode = "auto"`, `testpaths = ["tests"]`)
- `pytest-asyncio>=0.25` — async test support
- `pytest-httpserver>=1.1` — HTTP mock server for callback tests
- `pytest-xdist>=3.6` — parallel test execution

**Build/Dev:**
- `hatchling` — build backend (`pyproject.toml` `[build-system]`)
- `pyinstaller==6.13.0` — binary packaging (pinned in `build-binary.yml`)
- `ruff>=0.9` — linting and formatting; config in `pyproject.toml` (`line-length = 120`, `target-version = "py312"`)
- `pyright>=1.1.404` — static type checking

## Key Dependencies

**Critical:**
- `asyncpg>=0.30` — async Postgres driver; used in `ghosthands/integrations/database.py` for job queue, credentials, LISTEN/NOTIFY HITL signals
- `httpx>=0.28` — async HTTP client; used in `ghosthands/integrations/valet_callback.py` for VALET API callbacks
- `cryptography>=44.0` — AES-256-GCM credential decryption; both GH envelope and VALET scrypt formats in `ghosthands/integrations/credentials.py`
- `structlog>=24.4` — structured key-value logging throughout all modules; no `print()` or `logging.basicConfig()` used in app code
- `click>=8.3` — CLI framework underlying `ghosthands/cli.py` and `ghosthands/main.py`
- `cdp-use>=1.4` — Chrome DevTools Protocol integration (browser-use upstream dep)
- `redis>=5.2` — optional Redis Streams for step trace publishing in `ghosthands/step_trace.py`

**Infrastructure:**
- `aiohttp>=3.13` — async HTTP (browser-use dep)
- `anyio>=4.12` — async compatibility (browser-use dep)
- `pillow>=12.12` — screenshot image processing (browser-use dep)
- `pypdf>=6.6` — PDF resume parsing (browser-use dep)
- `python-docx>=1.2` — DOCX resume parsing (browser-use dep)
- `python-dotenv>=1.0` — `.env` file loading
- `pyotp>=2.9` — TOTP/2FA support in browser-use
- `markdownify>=1.2` — page content conversion for LLM prompts (browser-use dep)
- `posthog>=7.7` — product analytics (browser-use upstream dep; not used in ghosthands app code)
- `bubus>=1.5` — event bus (browser-use dep)
- `uuid7>=0.1` — UUIDv7 generation (browser-use dep)
- `portalocker>=2.10` — file locking (browser-use dep)
- `cloudpickle>=3.1` — serialization (browser-use dep)
- `psutil>=7.2` — process/resource monitoring (browser-use dep)
- `authlib>=1.6` — OAuth (browser-use dep)
- `rich>=14.0` — terminal UI (browser-use dep)

## Configuration

**Environment:**
- All GhostHands vars prefixed `GH_` (e.g., `GH_DATABASE_URL`, `GH_ANTHROPIC_API_KEY`)
- Loaded via `pydantic-settings` `BaseSettings` in `ghosthands/config/settings.py`
- `.env` file auto-loaded; `.env.example` present at repo root
- LLM provider API keys: `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY` (also accepted without `GH_` prefix)
- Critical vars: `GH_DATABASE_URL`, `GH_ANTHROPIC_API_KEY` (or `GH_GOOGLE_API_KEY`), `GH_CREDENTIAL_ENCRYPTION_KEY`
- Optional proxy vars: `GH_LLM_PROXY_URL`, `GH_LLM_RUNTIME_GRANT`

**Build:**
- `pyproject.toml` — package manifest, ruff config, pytest config
- `build/hand-x.spec` — PyInstaller spec for standalone binary build
- `Dockerfile` / `Dockerfile.fast` — Docker images based on `python:3.12-slim`
- `uv.lock` — deterministic dependency lockfile

## Platform Requirements

**Development:**
- Python 3.12+, `uv` package manager
- Playwright Chromium: `playwright install chromium`
- Postgres 14+ for job queue (`GH_DATABASE_URL`)
- Optional: Redis for step tracing (`GH_STEP_TRACE_REDIS_URL`)

**Production:**
- Docker (`python:3.12-slim` base + system Chromium)
- Standalone binary (PyInstaller; distributed via GitHub Releases) for Electron desktop embedding
- Platforms: macOS ARM (darwin-arm64) — primary build; Linux x64, Windows x64 — disabled in matrix but spec supported

---

*Stack analysis: 2026-03-24*
