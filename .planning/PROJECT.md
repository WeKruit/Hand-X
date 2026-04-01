# Hand-X

## What This Is

Hand-X is a brownfield browser automation engine for job applications. It runs both as a desktop-invoked CLI and as a server-side worker, fills ATS flows across platforms like Workday and Greenhouse, and integrates with VALET and GH Desktop for profile data, progress reporting, and review.

The current project focus is SPA page transition detection — making the browser-use agent reliably call domhand_fill on SPA page transitions by enriching page identity fingerprinting to detect content changes even when the URL stays the same.

## Core Value

A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## Requirements

### Validated

- ✓ Multi-platform ATS automation exists with DOM-first fill and targeted recovery — existing
- ✓ Desktop CLI execution exists with browser-open review flow — existing
- ✓ Worker execution exists with job claiming, callbacks, and HITL pause/resume — existing
- ✓ Applicant data can already be assembled from parsed resumes, application profiles, and QA answers — existing

### Active

- [ ] Lightweight page fingerprint (headings + buttons + form count) collected per step in browser_use
- [ ] `_page_identity()` includes fingerprint hash to detect SPA transitions
- [ ] PAGE UPDATE + compaction fires on SPA content changes (not just URL changes)
- [ ] Workday SPA transitions trigger domhand_fill on new page
- [ ] No false positives from conditional field reveals within a page

## Current Milestone: v1.2 SPA Page Transition Detection

**Goal:** Make the browser-use agent reliably call domhand_fill on SPA page transitions by fixing page transition detection to use DOM fingerprinting instead of URL-only comparison.

**Target features:**
- Page fingerprint JS eval per step (headings + buttons + form count) — ~3-5ms
- Fingerprint hash included in `_page_identity()` for SPA detection
- Existing PAGE UPDATE + compaction fires on SPA transitions
- All changes in browser_use core (generic, not ghosthands-specific)

### Out of Scope

- Programmatically executing domhand_fill from hooks — too aggressive, creates noise
- Per-action middleware intercepting every tool call — too aggressive
- Forcing domhand_fill via agent loop modification — changes browser-use agent contract
- Heading-only detection (h1/h2) — fragile, not generic enough

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
| Use DOM fingerprint (headings+buttons+forms) not heading-only | Rich multi-signal comparison avoids false positives from conditional fields | v1.2 |
| Changes in browser_use core, not ghosthands | Page transition detection is generic — any browser_use consumer benefits | v1.2 |
| Reuse existing PAGE UPDATE mechanism | Already works for URL changes on GS Oracle — just extend trigger to SPA | v1.2 |
| Rejected hook-per-step approach | Tried in v1.1, created noise — telling LLM what to do vs. giving it correct context | v1.2 |

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
*Last updated: 2026-03-31 after milestone v1.2 start*
