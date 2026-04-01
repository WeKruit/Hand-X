# Hand-X

## What This Is

Hand-X is a brownfield browser automation engine for job applications. It runs both as a desktop-invoked CLI and as a server-side worker, fills ATS flows across platforms like Workday and Greenhouse, and integrates with VALET and GH Desktop for profile data, progress reporting, and review.

The current project focus is streamlining the Desktop ↔ Hand-X binary integration — fixing the build pipeline so code changes on `main` reliably produce a working binary that Desktop can dispatch jobs to end-to-end.

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

## Current Milestone: v1.3 Streamlined Desktop ↔ Hand-X Integration

**Goal:** Make the Desktop→Hand-X binary pipeline reliable and streamlined — one command rebuilds from current source, installs, and Desktop runs jobs end-to-end.

**Target features:**
- Reliable binary build: dev-deploy.sh always uses project .venv, bundles all deps, installs to correct path
- Build verification: smoke test validates critical module imports before installing
- Current source in binary: all recent changes automatically included by rebuilding from main
- End-to-end validation: Desktop dispatches job → Hand-X binary runs → completes

### Out of Scope

- .dmg packaging for Mac app distribution — separate milestone after integration is stable
- CI/CD automated binary builds (GitHub Actions) — future, manual dev-deploy.sh is sufficient now
- Cross-platform binary testing (Windows/Linux) — macOS-only for now
- Changing Hand-X's LLM provider architecture — just ensure all providers are bundled correctly

## Context

Hand-X is already a large brownfield codebase with a documented architecture under `.planning/codebase/`. The codebase currently supports desktop CLI mode, worker mode, DOM-first filling, Stagehand escalation, runtime learning, and VALET/Postgres integration.

The current gap is the binary build pipeline. Hand-X works perfectly when run as Python source (`apply.sh`), but the PyInstaller binary that Desktop uses is stale (dev-20260328) and was built with wrong Python (anaconda 3.11 instead of project .venv 3.12), causing missing modules (openai, playwright, aiohttp). Recent code changes (field renames to authorized_to_work_in_us/needs_visa_sponsorship, timeout increases, citizenship_country/visa_type fields, LLM proxy config) are only in source, not in the binary.

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
*Last updated: 2026-04-01 after milestone v1.3 start*
