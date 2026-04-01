# Phase 10: Build Pipeline + Installation - Context

**Gathered:** 2026-04-01
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

Running `dev-deploy.sh` reliably produces a binary from the project .venv Python 3.12 with all required modules bundled, validates the binary with a smoke test, and installs it where Desktop expects it.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. Use ROADMAP phase goal, success criteria, and codebase conventions to guide decisions.

Key constraints from prior session:
- dev-deploy.sh must always `source .venv/bin/activate` (never skip based on VIRTUAL_ENV check — conda sets it)
- Primary install path is `~/Library/Application Support/gh-desktop-app/bin/` (not `Valet/bin/`)
- Alternate path `~/Library/Application Support/Valet/bin/` if it exists (copy there too)
- PyInstaller spec already has hidden_imports for openai, anthropic, google.genai — verify they're correct
- Smoke test should validate imports INSIDE the binary (not just file existence)

</decisions>

<code_context>
## Existing Code Insights

### Key Files
- `scripts/dev-deploy.sh` — Build and install script (previously had venv activation bug and wrong install path — both fixed)
- `build/hand-x.spec` — PyInstaller spec with hidden_imports, data files, exclusions
- `ghosthands/llm/client.py` — Dynamic LLM imports (openai, anthropic, google.genai)
- `browser_use/llm/__init__.py` — Lazy __getattr__ imports for chat model classes

### Known Issues (from previous session)
- conda sets VIRTUAL_ENV globally, old check `if [ -z "${VIRTUAL_ENV:-}" ]` skipped .venv activation
- Script installed to Valet/bin but Desktop reads from gh-desktop-app/bin
- Both bugs were fixed in the previous session but binary was never cleanly rebuilt

### Integration Points
- Desktop reads binary from `~/Library/Application Support/gh-desktop-app/bin/hand-x-darwin-arm64`
- Desktop reads version state from `~/Library/Application Support/gh-desktop-app/bin/hand-x-downloaded-version.json`

</code_context>

<specifics>
## Specific Ideas

No specific requirements — infrastructure phase. Refer to ROADMAP phase description and success criteria.

</specifics>

<deferred>
## Deferred Ideas

None — infrastructure phase.

</deferred>
