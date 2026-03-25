# Coding Conventions

**Analysis Date:** 2026-03-24

## Naming Patterns

**Files:**
- `snake_case.py` throughout: `fill_executor.py`, `fill_profile_resolver.py`, `cost_tracker.py`
- Module files named for their primary concern: `dropdown_fill.py`, `dropdown_match.py`, `dropdown_verify.py`
- Test files prefixed `test_`: `test_domhand_fixes.py`, `test_stagehand_layer.py`
- Private helpers prefixed `_`: `_heartbeat_loop`, `_fill_single_field`, `_dispatch_platform_fill`
- Constants in `SCREAMING_SNAKE_CASE`: `HEARTBEAT_INTERVAL`, `MAX_FILL_ROUNDS`, `POST_OPTION_CLICK_SETTLE_S`

**Functions:**
- `snake_case` for all functions and methods
- Async functions named the same as sync counterparts — async is the default: `async def execute_job(...)`, `async def _heartbeat_loop(...)`
- Private module-level helpers prefixed `_`: `_fill_text_field`, `_known_profile_value`, `_dispatch_platform_fill`
- Factory functions prefixed `create_` or `build_`: `create_job_agent()`, `build_system_prompt()`, `build_cost_summary()`
- Boolean helpers prefixed with `is_`, `has_`, `can_`, `should_`: `is_domhand_retry_capped()`, `has_cached_semantic_alias()`

**Variables:**
- `snake_case` everywhere
- Type-annotated with modern Python 3.12 syntax: `str | None`, `list[str]`, `dict[str, Any]`
- Timeout constants suffixed with units: `_TIMEOUT_MS`, `_SETTLE_S`, `_INTERVAL`, `_TTL_SECONDS`

**Types/Classes:**
- `PascalCase` for classes: `FormField`, `FieldFillOutcome`, `StepHooks`, `CostTracker`
- Pydantic models for all external data: `FormField(BaseModel)`, `FillFieldResult(BaseModel)`, `CanonicalProfile(BaseModel)`
- `dataclass(frozen=True)` for lightweight immutable results: `FieldFillOutcome`, `StepCost`, `CostSnapshot`
- `Literal["a", "b"]` for constrained string types: `ProfileProvenance = Literal["explicit", "derived", "policy"]`
- `TypeAlias` / `type` for named aliases where appropriate

**JavaScript constants embedded in Python:**
- ALL_CAPS with `_JS` suffix: `_FILL_FIELD_JS`, `SCAN_VISIBLE_OPTIONS_JS`, `CLICK_COMBOBOX_TOGGLE_BY_FFID_JS`

## Code Style

**Formatting:**
- Tool: `ruff format` (via `pyproject.toml`)
- Line length: 120 characters
- Target: Python 3.12

**Linting:**
- Tool: `ruff check` with rules `["E", "F", "I", "N", "W", "UP", "B", "SIM", "RUF"]`
- `E501` (line-too-long) is ignored — the 120-char limit is enforced by formatter only
- `isort` integrated via ruff; `ghosthands` is declared as first-party

**Pre-commit:** `pre-commit>=4.2` is a dev dependency; hooks are configured

## Import Organization

**Order (enforced by ruff isort):**
1. Standard library (`asyncio`, `contextlib`, `json`, `os`, `re`)
2. Third-party (`structlog`, `pydantic`, `playwright`, `anthropic`)
3. First-party — `browser_use.*` (vendored in-tree, treated as third-party by isort)
4. First-party — `ghosthands.*`

**`from __future__ import annotations`:**
- Present in 40 of 40 `ghosthands/` modules — mandatory for forward reference support
- Always the first import, before all others

**Path Aliases:** None — all imports use full dotted paths

**Late imports to break circular references:**
- Pattern used in `fill_executor.py` and `fill_profile_resolver.py` for delegates that create circular deps:
  ```python
  def _preferred_field_label(field: FormField) -> str:
      from ghosthands.dom.fill_label_match import _preferred_field_label as _impl
      return _impl(field)
  ```
- Module-level `if TYPE_CHECKING:` block for type-only imports: `from ghosthands.actions.domhand_fill import ResolvedFieldValue`

## Error Handling

**Strategy:** Explicit exception handling with structured logging. Bare `except Exception` used only at top-level boundaries (job executor, heartbeat loop) to prevent single-job failures from crashing the worker.

**Patterns:**
- Custom exception classes for domain errors: `BudgetExceededError(Exception)`, `StepLimitExceededError(Exception)` — defined in `ghosthands/worker/cost_tracker.py`
- `asyncio.CancelledError` is always re-raised after cleanup: `except asyncio.CancelledError: ... raise`
- Swallowed exceptions use `contextlib.suppress()` or explicit bare `except Exception: pass` for optional side effects (e.g., cursor visuals)
- Functions that can fail gracefully return `None` or a typed result object rather than raising
- DOM/browser operations wrapped in `try/except Exception` and returned as `False` or empty; caller decides semantics

**Example from `ghosthands/worker/executor.py`:**
```python
try:
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    return any(...)
except Exception:
    return False
```

## Logging

**Framework:** `structlog` for all `ghosthands/` code. `logging` (stdlib) used in `factory.py` and `hooks.py` where browser-use compatibility is needed.

**Logger instantiation:**
```python
import structlog
logger = structlog.get_logger(__name__)      # ghosthands modules
# OR
import logging
logger = logging.getLogger(__name__)          # agent/factory.py, agent/hooks.py
```

**Log call style — always key-value pairs with an event string first:**
```python
logger.info("executor.heartbeat_failed", job_id=job_id, error=str(exc))
logger.debug("handx.env_loaded", extra={"path": str(path)})
logger.warning("hitl.wait_timeout", job_id=job_id, timeout=timeout)
```

**Event naming convention:** `{module}.{event}` dot-separated: `"ghosthands.stopped"`, `"hitl.poll_timeout"`, `"cursor_visual.injected"`

**No `print()` calls in application code** — stray prints are redirected to stderr by `install_stdout_guard()` in `ghosthands/output/jsonl.py`

## Comments and Docstrings

**Module docstrings:**
- Present on all `ghosthands/` modules — describe purpose and key responsibilities
- Multi-paragraph for complex modules (e.g., `domhand_fill.py` has a 20-line docstring with numbered steps)
- Include usage examples with `::` RST syntax in factory modules: `ghosthands/agent/factory.py`

**Inline comments:**
- Section dividers with `# ── Section Name ────────────────────────────────────────────────` to delineate logical sections within long modules
- `# noqa` or type-ignore comments used sparingly and intentionally

**Function docstrings:**
- NumPy-style for complex public functions in `factory.py` (Parameters / Returns blocks)
- Plain text for most private helpers — one line sufficient
- No docstrings on obvious getters/setters

## Function Design

**Size:** Functions are kept focused on one concern. Large actions like `domhand_fill` are split into focused sub-modules (`fill_executor.py`, `fill_profile_resolver.py`, `fill_verify.py`, `fill_label_match.py`).

**Parameters:**
- Keyword-only args enforced with `*` for boolean flags: `def _fill_outcome(success: bool, *, matched_label: str | None = None)`
- Settings accessed via `settings` singleton (`ghosthands/config/settings.py`) rather than passed as params to deep helpers
- Pydantic models for complex parameter bundles: `DomHandFillParams`, `DomHandSelectParams`

**Return Values:**
- Type-annotated always
- Dataclasses for structured results: `FieldFillOutcome`, `CostSnapshot`
- `bool | None` returned from dispatch functions where `None` = "not handled" (distinct from failure)
- Never raise for expected "not found" or "no match" cases — return `None` or empty

## Module Design

**Exports:**
- No `__all__` declarations — modules export by convention (public = no leading underscore)
- Private helpers prefixed `_` are still importable for testing (tests import them directly)

**Barrel Files:** Not used. Direct module imports throughout.

**Settings singleton:** `ghosthands/config/settings.py` exposes `settings = Settings()` module-level singleton. All modules import `from ghosthands.config.settings import settings`.

**Vendored library:** `browser_use/` is vendored in-tree (not installed from PyPI). Imported as `from browser_use.xxx import yyy` — treated as project code, not a dependency.

---

*Convention analysis: 2026-03-24*
