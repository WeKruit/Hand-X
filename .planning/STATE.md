# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-02)

**Core value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.
**Current focus:** Milestone v1.4 -- Production Ship (Phase 12: VALET Prod Promotion)

## Current Position

Phase: 12 of 14 (VALET Prod Promotion)
Plan: 0 of 2 in current phase
Status: Ready to plan
Last activity: 2026-04-02 -- v1.4 roadmap created (Phases 12-14)

Progress: [========================░░░░░░] 78% milestones defined (11/14 phases scoped, 3 new)

## Accumulated Context

### Decisions

- Binary at `~/Library/Application Support/Valet/bin/hand-x-darwin-arm64` is stale (dev-20260328), built with anaconda Python 3.11
- Desktop reads from `~/Library/Application Support/gh-desktop-app/bin/` (primary) and `~/Library/Application Support/Valet/bin/` (alternate)
- dev-deploy.sh had two bugs fixed: (1) conda VIRTUAL_ENV tricked venv activation skip, (2) installed to Valet instead of gh-desktop-app
- VALET staging has 289 commits not in prod -- includes profile endpoint, DB migrations, resume parser, write-path whitelist
- languages column changed from varchar(2000) to jsonb in staging -- prod ALTER requires data verification
- Rollback SHA for VALET prod: 81ce921
- VALET API as single source of truth for profile data (v1.4 decision)
- Cherry-pick or full promote for staging->prod -- 289 commits, verify DB migration safety first (v1.4 decision)

### Pending Todos

None yet.

### Blockers/Concerns

- Prod DB languages column data must be verified before jsonb ALTER (blocks Phase 12)
- 289-commit staging->prod delta is large blast radius (mitigated by rollback SHA)
- Desktop Workday role-gating is on feat/staging-dmg-packaging branch, not yet committed to main

## Session Continuity

Last session: 2026-04-02
Stopped at: v1.4 roadmap created, ready to plan Phase 12
Resume file: None
