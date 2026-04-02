# Hand-X

## What This Is

Hand-X is a brownfield browser automation engine for job applications. It runs both as a desktop-invoked CLI and as a server-side worker, fills ATS flows across platforms like Workday and Greenhouse, and integrates with VALET and GH Desktop for profile data, progress reporting, and review.

The current project focus is rebuilding the observation layer from the ground up — replacing the DOM-based field extraction with a deterministic, generic system that reliably understands page structure, field semantics, grouping, and state across all ATS platforms.

## Core Value

A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## Requirements

### Validated

- ✓ Multi-platform ATS automation exists with DOM-first fill and targeted recovery — existing
- ✓ Desktop CLI execution exists with browser-open review flow — existing
- ✓ Worker execution exists with job claiming, callbacks, and HITL pause/resume — existing
- ✓ Applicant data can already be assembled from parsed resumes, application profiles, and QA answers — existing

### Active

- [ ] Observation layer produces deterministic, generic semantic representation of any ATS form page
- [ ] Field grouping correctly associates questions with options regardless of DOM structure
- [ ] State detection reliably knows whether fields are filled, checkboxes checked, dropdowns selected
- [ ] Semantic understanding maps structural variations to canonical meanings across platforms
- [ ] Repeater sections are recognized and bounded without platform-specific logic
- [ ] Clean observer contract defines interface between observation, decision, and action layers

## Current Milestone: v2.0 Observation Layer Rebuild

**Goal:** Ground-up rebuild of the observation layer so Hand-X can deterministically and generically understand page structure, field semantics, grouping, and state across all ATS platforms.

**Target features:**
- Deterministic page observation: same page always produces the same semantic representation
- Generic field grouping: correctly associates questions with options, sections with fields, regardless of DOM structure
- Reliable state detection: knows field state even in custom widgets
- Semantic understanding: maps structural variations to meaning across platforms
- Repeater awareness: recognizes repeatable sections and their boundaries generically
- Clean observer contract: well-defined interface between observation, decision, and action layers

### Out of Scope

- .dmg packaging for Mac app distribution — separate milestone after integration is stable
- CI/CD automated binary builds (GitHub Actions) — future, manual dev-deploy.sh is sufficient now
- Cross-platform binary testing (Windows/Linux) — macOS-only for now
- Changing Hand-X's LLM provider architecture — just ensure all providers are bundled correctly

## Context

Hand-X is already a large brownfield codebase with a documented architecture under `.planning/codebase/`. The codebase currently supports desktop CLI mode, worker mode, DOM-first filling, Stagehand escalation, runtime learning, and VALET/Postgres integration.

The current observation layer (`ghosthands/dom/field_extractor.py`) injects JavaScript to traverse the full DOM including shadow DOMs, discovers interactive elements by querying for native inputs + ARIA roles + custom widgets, and resolves labels via an accessibility chain. Grouping uses field_type + label + section matching. This approach is non-deterministic (DOM structure doesn't encode semantic meaning reliably), non-generic (can't handle structural variations across ATS platforms), and overwhelms the LLM (Gemini 3.0 Flash) with too much raw DOM context, causing hallucinations and wrong field values. Specific failure modes include: sibling elements that are semantically related (multiselect options as siblings to questions) being misinterpreted, state changes not being detected in custom widgets, and the agent looping on already-completed actions because it can't observe that a selection was made.

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
| Fix build pipeline before adding features | Stale binary is blocking all Desktop testing — no point adding features nobody can run | v1.3 |
| dev-deploy.sh always activates .venv | Conda sets VIRTUAL_ENV, tricking the old check into skipping project venv | v1.3 |
| Primary install path is gh-desktop-app not Valet | Desktop reads from ~/Library/Application Support/gh-desktop-app/bin/ | v1.3 |
| Rebuild observation layer from ground up | Current DOM extraction is non-deterministic, non-generic, causes hallucinations — fixing incrementally won't solve the structural issues | v2.0 |
| Action layer stays, observation is replaced | Acting (domhand_fill strategies) works well — the problem is observation feeding wrong data to good actors | v2.0 |
| Strategic screenshots OK, per-action screenshots not | Screenshots too expensive per-action but can be used per-page/section for anchoring | v2.0 |

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
*Last updated: 2026-04-02 after milestone v2.0 start*
