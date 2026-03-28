# Phase 1: Runtime Contract In VALET - Context

**Gathered:** 2026-03-24
**Status:** Ready for planning

<domain>
## Phase Boundary

Make VALET the authoritative producer of hydrated applicant runtime profile data for a selected user/resume context. This phase defines and implements the producer-side contract boundary; it does not yet complete all downstream consumer migration work.

</domain>

<decisions>
## Implementation Decisions

### Source of truth
- **D-01:** VALET must provide one hydrated runtime-profile interface instead of Hand-X and Desktop maintaining separate merge logic.
- **D-02:** Hand-X should become a consumer of runtime profile data, not a second owner of merge policy.

### Runtime payload scope
- **D-03:** The runtime payload must include parsed resume data, global `user_application_profiles`, resume-specific `resume_application_profiles`, and always-use QA answers.
- **D-04:** Selected resume context is part of the runtime contract boundary; resume selection must not remain an unrelated local default.
- **D-05:** Platform credentials and auth intent are part of the runtime delivery boundary and must not rely on CLI-arg secret passing.

### Migration guardrails
- **D-06:** Do not ship a compatibility or patch-layer architecture that preserves duplicated long-term merge logic across repos.
- **D-07:** Producer/consumer responsibilities must be explicit enough that Desktop, Hand-X CLI, and worker mode can converge on the same contract safely.

### the agent's Discretion
- Exact endpoint naming and route shape
- Exact payload field naming, as long as the ownership boundary and included data are unambiguous
- Whether the contract returns direct secrets, references, or a brokered credential structure, provided secrets are not exposed unsafely

</decisions>

<specifics>
## Specific Ideas

- The current temporary Hand-X DB-backed reconstruction is acceptable only as a migration seam, not as the final architecture.
- The target is parity with Desktop behavior, but with VALET owning the hydrated runtime payload instead of Desktop owning the merge.

</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Hand-X architecture
- `.planning/PROJECT.md` — project-level scope, core value, and active requirements
- `.planning/ROADMAP.md` — Phase 1 goal, success criteria, and plan slots
- `.planning/REQUIREMENTS.md` — PROF-01 through PROF-03 define the required outcome
- `.planning/STATE.md` — current project position and blocker summary
- `.planning/codebase/ARCHITECTURE.md` — current Hand-X execution model and layer boundaries
- `.planning/codebase/INTEGRATIONS.md` — current external integrations and data movement seams
- `.planning/codebase/CONCERNS.md` — known risks that planning should account for

### Existing product and bridge docs
- `PRD-DESKTOP-BRIDGE.md` — current Desktop ↔ Hand-X bridge expectations
- `AGENTS.md` — project-specific implementation constraints and first-principles rules

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ghosthands/integrations/database.py`: current raw Postgres access used by Hand-X
- `ghosthands/integrations/resume_loader.py`: current temporary runtime-profile reconstruction logic in Hand-X
- `ghosthands/cli.py`: current local testing entry point and profile source selection logic

### Established Patterns
- Desktop currently hydrates profile data before handing off to Hand-X
- Hand-X already supports environment/file-based secret passing and should keep avoiding CLI-arg secret leakage

### Integration Points
- VALET API/service layer will own the new hydrated runtime contract
- Desktop and Hand-X loaders will be the first consumers to migrate

</code_context>

<deferred>
## Deferred Ideas

- Full consumer cutover across all Hand-X entry paths belongs to later phases
- Broader credential lifecycle cleanup beyond runtime delivery semantics belongs to later phases

</deferred>

---
*Phase: 01-runtime-contract-in-valet*
*Context gathered: 2026-03-24*
