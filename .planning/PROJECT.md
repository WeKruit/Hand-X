# Hand-X

## What This Is

Hand-X is a brownfield browser automation engine for job applications. It runs both as a desktop-invoked CLI and as a server-side worker, fills ATS flows across platforms like Workday and Greenhouse, and integrates with VALET and GH Desktop for profile data, progress reporting, and review.

The current project focus is to remove runtime-profile drift between VALET, GH Desktop, and Hand-X by making VALET the single source of truth for the hydrated applicant runtime payload.

## Core Value

A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## Requirements

### Validated

- ✓ Multi-platform ATS automation exists with DOM-first fill and targeted recovery — existing
- ✓ Desktop CLI execution exists with browser-open review flow — existing
- ✓ Worker execution exists with job claiming, callbacks, and HITL pause/resume — existing
- ✓ Applicant data can already be assembled from parsed resumes, application profiles, and QA answers — existing

### Active

- [ ] VALET provides a single hydrated runtime-profile contract for selected applicant + resume context
- [ ] Desktop, worker, and local Hand-X test flows consume the same runtime payload shape
- [ ] Selected resume asset and platform credentials travel through the same contract instead of repo-local reconstruction
- [ ] Contract parity is proven by regression tests, not inferred from duplicate merge logic

### Out of Scope

- Rewriting the browser automation engine — not required to eliminate profile drift
- Replacing browser-use or the DOM-first fill stack — unrelated to the current source-of-truth problem
- Introducing client-side Supabase session logic into Hand-X — Hand-X should consume a server-provided contract, not own auth/session resolution

## Context

Hand-X is already a large brownfield codebase with a documented architecture under `.planning/codebase/`. The codebase currently supports desktop CLI mode, worker mode, DOM-first filling, Stagehand escalation, runtime learning, and VALET/Postgres integration.

The current architectural gap is not ATS automation capability; it is ownership of applicant runtime data. Today, GH Desktop hydrates a profile and passes it to Hand-X, while Hand-X can also reconstruct a runtime profile directly from VALET data. That duplication creates drift risk for applicant identity, resume selection, QA answers, and credential handling.

## Constraints

- **Architecture**: VALET must become the single source of truth for hydrated runtime profile data — duplicated merge logic across repos is not acceptable
- **Security**: Secrets and applicant PII must not be passed on CLI args — current env/file-based handling must be preserved
- **Compatibility**: Desktop mode, worker mode, and local testing must all remain supported while moving to the unified contract
- **Testing**: Contract parity must be proven with automated tests before consumers switch fully

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Make VALET provide one hydrated runtime-profile interface | Eliminates duplicated merge logic and data drift between Desktop and Hand-X | — Pending |
| Keep Hand-X as a consumer of applicant runtime data, not an owner of merge policy | Preserves a single source of truth and reduces cross-repo divergence | — Pending |
| Treat selected resume asset and auth credentials as part of runtime delivery, not ad hoc local defaults | End-to-end parity requires identity, file, and auth context to move together | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition**:
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone**:
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-24 after initialization*
