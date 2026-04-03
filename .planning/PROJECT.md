# Hand-X

## What This Is

Hand-X is a brownfield browser automation engine for job applications. It runs both as a desktop-invoked CLI and as a server-side worker, fills ATS flows across platforms like Workday and Greenhouse, and integrates with VALET and GH Desktop for profile data, progress reporting, and review.

The current project focus is shipping the unified data contract and all staging work to production — VALET staging→prod promotion with DB migration safety, Desktop release with Workday role-gating, and Hand-X binary rebuild from current source.

## Core Value

A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## Requirements

### Validated

- ✓ Multi-platform ATS automation exists with DOM-first fill and targeted recovery — existing
- ✓ Desktop CLI execution exists with browser-open review flow — existing
- ✓ Worker execution exists with job claiming, callbacks, and HITL pause/resume — existing
- ✓ Applicant data can already be assembled from parsed resumes, application profiles, and QA answers — existing

### Active

- [ ] dev-deploy.sh reliably builds from project .venv Python 3.12 and installs to correct Desktop path
- [ ] Binary bundles all required modules (openai, playwright, aiohttp, google-genai, anthropic)
- [ ] Smoke test validates critical imports before installing binary
- [ ] Desktop dispatches a job and Hand-X binary completes without module errors
- [ ] All recent code changes (field renames, timeout increases, profile fields) reflected in binary

## Current Milestone: v1.4 Production Ship

**Goal:** Promote all staging work to production across VALET, Desktop, and Hand-X — DB migrations verified, unified data contract live, Desktop release cut.

**Target features:**
- VALET staging → prod: cherry-pick or full promote of profile endpoint, DB migrations (13 columns + jsonb), resume parser, write-path whitelist
- DB migration safety: verify prod data compatibility before ALTER (languages varchar→jsonb)
- Desktop: commit Workday role-gating + packaging changes, merge to main, build release DMG
- Hand-X binary: rebuild from current source so Desktop ships with latest fills
- E2E validation: Desktop-dispatched job completes on prod with all profile fields populated

### Out of Scope

- New ATS platform support — ship what works now
- CI/CD automated binary builds — manual dev-deploy.sh is sufficient
- Cross-platform (Windows/Linux) — macOS-only for now
- Matching engine / job recommendations in prod — not required for core automation

## Context

Hand-X is already a large brownfield codebase with a documented architecture under `.planning/codebase/`. The codebase currently supports desktop CLI mode, worker mode, DOM-first filling, Stagehand escalation, runtime learning, and VALET/Postgres integration.

Staging is stable. VALET has 289 commits on staging not yet in prod, including: unified profile API endpoint, DB schema additions (13 new application profile columns, languages varchar→jsonb), resume parser improvements, write-path whitelist expansion, and the full @wekruit/valet-shared@1.0.3 contract. Desktop has uncommitted Workday role-gating and packaging changes on `feat/staging-dmg-packaging`. Hand-X source on main has all profile/EEO fixes committed. The binary needs rebuilding from current source.

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
| VALET API as single source of truth for profile data | Desktop and apply.sh must use identical normalization path — duplicated merge logic causes regressions | v1.4 |
| Cherry-pick or full promote for staging→prod | 289 commits; verify DB migration safety before promoting | v1.4 |

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
*Last updated: 2026-04-02 after milestone v1.4 start*
