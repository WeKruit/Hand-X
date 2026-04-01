# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-01)

**Core value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.
**Current focus:** Milestone v1.3 -- Streamlined Desktop ↔ Hand-X Integration

## Current Position

Phase: Not started (defining requirements)
Plan: —
Status: Defining requirements
Last activity: 2026-04-01 — Milestone v1.3 started

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: -
- Total execution time: 0.0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: -
- Trend: Stable

## Accumulated Context

### Decisions

- Binary at `~/Library/Application Support/Valet/bin/hand-x-darwin-arm64` is stale (dev-20260328), built with anaconda Python 3.11
- Desktop reads from `~/Library/Application Support/gh-desktop-app/bin/` (primary) and `~/Library/Application Support/Valet/bin/` (alternate)
- dev-deploy.sh had two bugs fixed in previous session: (1) conda VIRTUAL_ENV tricked venv activation skip, (2) installed to Valet instead of gh-desktop-app
- apply.sh works because it runs Python source directly — all packages available
- When Gemini returns bad JSON, browser-use error handler tries `import openai` → missing from binary → fatal crash
- Profile fields renamed: work_authorization→authorized_to_work_in_us, visa_sponsorship→needs_visa_sponsorship — only in source, not binary
- New fields added: citizenship_country, visa_type, citizenship_status, us_citizen, export_control_eligible — only in source
- Timeout increases on LLM calls — only in source, not binary
- PyInstaller spec (build/hand-x.spec) has hidden_imports for openai, anthropic, google.genai — but binary was built with wrong Python so deps weren't found

### Pending Todos

None yet.

### Blockers/Concerns

None.

## Session Continuity

Last session: 2026-04-01
Stopped at: Milestone v1.3 started, defining requirements
Resume file: None
