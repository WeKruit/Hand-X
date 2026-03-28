# Requirements: Hand-X

**Defined:** 2026-03-24
**Core Value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## v1 Requirements

### Runtime Profile Contract

- [ ] **PROF-01**: VALET exposes one authenticated interface that returns a hydrated runtime profile for a selected user/resume context
- [ ] **PROF-02**: The hydrated payload includes parsed resume data, global application defaults, resume-specific application overrides, and always-use QA answers
- [ ] **PROF-03**: The hydrated payload schema is documented and versioned enough for multiple consumers to rely on it safely

### Resume And Credential Delivery

- [ ] **RUNT-01**: The selected resume file for the run is resolved through the same VALET-owned contract path, not local Hand-X defaults
- [ ] **RUNT-02**: Platform auth credentials can be delivered to consumers without exposing secrets on CLI arguments
- [ ] **RUNT-03**: The runtime contract distinguishes auth intent consistently, including existing-account and create-account flows

### Consumer Integration

- [ ] **CONS-01**: GH Desktop and Hand-X consume the same runtime payload shape for applicant identity data
- [ ] **CONS-02**: Worker mode and local deterministic testing can use the same runtime source-of-truth contract
- [ ] **CONS-03**: Overriding login credentials for platform testing does not silently mutate applicant contact data in the profile

### Verification And Regression Safety

- [ ] **QUAL-01**: Automated tests prove payload parity and merge precedence across global defaults, resume-specific overrides, and QA answers
- [ ] **QUAL-02**: Missing required runtime data surfaces explicit blockers instead of silent fallback behavior
- [ ] **QUAL-03**: Consumer migration preserves current desktop, worker, and CLI execution flows during the rollout

## v2 Requirements

### Broader Runtime Delivery

- **NEXT-01**: Resume download and hydrated runtime profile can be fetched through one end-to-end API handoff instead of separate calls
- **NEXT-02**: Stored platform credentials can be retrieved through the same VALET-owned contract with explicit authorization and auditability

## Out of Scope

| Feature | Reason |
|---------|--------|
| Rebuilding ATS automation strategies | The current problem is runtime data ownership, not fill-engine capability |
| Moving Hand-X to direct Supabase client/session auth | Hand-X should remain a consumer of server-owned runtime contracts |
| Broad schema redesign of all applicant data models | Not required to establish one runtime interface and migrate consumers |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| PROF-01 | Phase 1 | Pending |
| PROF-02 | Phase 1 | Pending |
| PROF-03 | Phase 1 | Pending |
| RUNT-01 | Phase 2 | Pending |
| RUNT-02 | Phase 2 | Pending |
| RUNT-03 | Phase 2 | Pending |
| CONS-01 | Phase 3 | Pending |
| CONS-02 | Phase 3 | Pending |
| CONS-03 | Phase 3 | Pending |
| QUAL-01 | Phase 4 | Pending |
| QUAL-02 | Phase 4 | Pending |
| QUAL-03 | Phase 4 | Pending |

**Coverage:**
- v1 requirements: 12 total
- Mapped to phases: 12
- Unmapped: 0 ✓

---
*Requirements defined: 2026-03-24*
*Last updated: 2026-03-24 after initialization*
