# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-02)

**Core value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.
**Current focus:** Milestone v1.4 — Production Ship

## Current Position

Phase: Not started (defining requirements)
Plan: —
Status: Defining requirements
Last activity: 2026-04-02 — Milestone v1.4 started

## Accumulated Context

### Decisions

- Binary at `~/Library/Application Support/Valet/bin/hand-x-darwin-arm64` is stale (dev-20260328), built with anaconda Python 3.11
- Desktop reads from `~/Library/Application Support/gh-desktop-app/bin/` (primary) and `~/Library/Application Support/Valet/bin/` (alternate)
- dev-deploy.sh had two bugs fixed in previous session: (1) conda VIRTUAL_ENV tricked venv activation skip, (2) installed to Valet instead of gh-desktop-app
- apply.sh works because it runs Python source directly -- all packages available
- VALET staging has 289 commits not in prod — includes profile endpoint, DB migrations, resume parser, write-path whitelist
- languages column changed from varchar(2000) to jsonb in staging — prod ALTER requires data verification
- @wekruit/valet-shared@1.0.3 published with all profile fields
- Desktop has uncommitted Workday role-gating on feat/staging-dmg-packaging branch
- Unified profile API endpoint deployed to staging, not yet prod-verified

### Pending Todos

None yet.

### Blockers/Concerns

- Prod DB languages column data must be verified before jsonb ALTER
- 289-commit staging→prod delta is large blast radius

## Session Continuity

Last session: 2026-04-02
Stopped at: Milestone v1.4 started, defining requirements
Resume file: None
