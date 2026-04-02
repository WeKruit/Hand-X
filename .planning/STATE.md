# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-02)

**Core value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.
**Current focus:** Milestone v2.0 — Observation Layer Rebuild

## Current Position

Phase: Not started (defining requirements)
Plan: —
Status: Defining requirements
Last activity: 2026-04-02 — Milestone v2.0 started

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
- Current observation layer (field_extractor.py) is being replaced, not fixed — DOM traversal approach is fundamentally flawed
- Action layer (domhand_fill, fill strategies) stays — observation is the problem, not acting
- Screenshots can be used strategically (per-page/per-section) but not per-action due to cost
- Gemini 3.0 Flash hallucinates on large DOM context — new observation must manage context size
- OOD contract between observer, decision maker, and actor needs clean interfaces

### Pending Todos

None yet.

### Blockers/Concerns

None.

## Session Continuity

Last session: 2026-04-02
Stopped at: Milestone v2.0 started, running research
Resume file: None
