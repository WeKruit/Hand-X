---
phase: DomHand v3 Parity & Agent-Visible Verification
reviewers: [codex-gpt54, gemini, claude-opus-code-reviewer]
reviewed_at: 2026-03-27T00:15:00Z
plans_reviewed: [4-phase DomHand v3 parity implementation plan]
---

# Cross-AI Plan Review -- DomHand v3 Parity

## Codex Review (GPT-5.4)

**Risk Assessment: MEDIUM**

### Summary

The plan is directionally correct and the phase order is mostly right: Phase 1 attacks the highest-leverage defect, Phase 2 demotes vision to fallback, and Phase 3 is optional instead of a mandatory brownfield rewrite. The main issue is that Phase 1 still underspecifies the actual contract boundary. Today, `domhand_fill` hides the useful structure in metadata and feeds the agent prose-only summaries, while `fill_verify` still returns `success=True` for readback-unverified outcomes. Unless Phase 1 simultaneously defines a non-lossy review taxonomy, makes that review model-visible, and aligns `domhand_assess_state` plus the prompts with the same semantics, you can land "better JSON" without actually fixing the planner gap.

### Strengths

- Phase order is good. Phase 1 gives immediate behavioral value before any refactor.
- The premise is correct: verification must move back to deterministic DOM/CDP review, not screenshots.
- Phase 2 is conceptually right in demoting multimodal verification to a last resort instead of a primary tie-breaker.
- The plan keeps answer synthesis in the match/process layer and does not confuse it with verification.
- Phase 3 being optional is the right instinct for a brownfield codebase.
- The risk note on Oracle is honest: short-term reported failures may rise, and that is preferable to silently lying about verification.

### Concerns

- **HIGH -- Review stays in metadata, not model-visible.** Phase 1 will not fix the agent gap if review stays only in metadata. Current `domhand_fill` returns prose in `extracted_content` and `long_term_memory`, while structured JSON stays in metadata at domhand_fill.py:3698 and :3790. The agent history manager appends `long_term_memory`, not arbitrary metadata, at service.py:381.

- **HIGH -- `verified: bool` is too lossy.** Current runtime already distinguishes DOM-verified, LLM-verified, Stagehand-rescued, and "executor succeeded but readback never matched" at fill_verify.py:399. A boolean collapses unreadable Oracle/custom-widget states into the same bucket as true mismatches.

- **HIGH -- `domhand_fill` and `domhand_assess_state` will diverge.** Unless they share the same review engine. `assess_state` already has its own semantic matcher at domhand_assess_state.py:300 and suppresses `domhand_unverified` custom-select blockers at :857.

- **HIGH -- Prompts contradict the new review contract.** Current prompts explicitly tell the agent to trust visual observation over DOM for custom selects at prompts.py:540 and :658. Worker says not to let mismatched/unverified readback noise block advancement at executor.py:447. If those rules are not updated in the same phase, the new structured review will be contradicted by the runtime.

- **MEDIUM -- Phase 2 over-eager to add LLM equivalence.** There is already deterministic semantic matching in `assess_state` at domhand_assess_state.py:300 and :1408. Porting TS normalization and alias rules should come before invoking a model.

- **MEDIUM -- `long_term_memory` JSON risks prompt bloat and PII leakage.** Appended verbatim to agent history at service.py:381, and metadata exposed in hooks/logging at hooks.py:455. Raw `actual_read` for email, address, phone, DOB, or essay fields increases sensitive-data exposure.

- **MEDIUM -- Proposed taxonomy is incomplete.** Need to distinguish at least `verified`, `mismatch`, `unreadable/inconclusive`, and `unsupported`. Separately, page-level counts still need `already_filled` and `skipped/not_attempted`.

- **MEDIUM -- A single `actual_read: str` is not enough.** Multi-select, checkbox groups, grouped dates, and file uploads need typed review payloads, not forced stringification.

- **MEDIUM -- Oracle HCM risk is larger than stated.** `cx-select`, shadow/overlay widgets, and async display-label commits often produce empty or opaque readback. `readback_mismatch` will over-report unless Oracle-specific read probes exist.

- **LOW -- Module-global and page-scoped-guard findings already addressed.** Browser-session scoped fill state at domhand_fill.py:2710, invariants enforced in test_premises.py:819 and :856.

### Suggestions

1. Replace `verified: bool` with two axes: `execution_status` (`already_settled|executed|execution_failed|not_attempted`) and `review_status` (`verified|mismatch|unreadable|unsupported`).
2. Pull `verification_engine.py` forward into Phase 1, not Phase 3. `domhand_fill` and `domhand_assess_state` should call the same review code.
3. Put a capped factual review digest in the model-visible channel. Full JSON in metadata for tooling; agent gets only counts plus top-N rows.
4. Do not put raw per-field values into persistent memory. Mask PII fields, summarize long textarea values.
5. Keep Phase 2 text-only equivalence in a separate module from screenshot escalation.
6. Exhaust deterministic normalization before LLM equivalence (country aliases, phone formatting, date normalization, select display-label mapping).
7. Limit LLM equivalence to low-risk display-string comparisons.
8. Define field-family review payloads now: scalar text/select, token-set multi-select, composite grouped date, boolean/group choice, file attachment.
9. Add Oracle-specific deterministic read adapters before classifying rows as mismatch.
10. Expand tests beyond JSON shape: parity tests between `domhand_fill` and `domhand_assess_state`, token-budget tests, directive-free tests, PII redaction tests.

---

## Gemini Review

**Risk Assessment: MEDIUM**

### Summary

The plan is a sophisticated evolution that correctly prioritizes deterministic truth over LLM-driven heuristics. By introducing a formal "Review" phase and a three-valued outcome taxonomy, it addresses the most critical flaw: the "silent happy path" where unverified fills are indistinguishable from verified ones. The move toward text-only semantic equivalence is a brilliant cost and latency optimization, and the structural alignment with v3 provides necessary architectural cleanup for long-term maintainability.

### Strengths

- **Taxonomy Correction:** Moving from binary `success` to a status reflecting DOM readback reality is the single most important change for agent reliability.
- **Cost-Efficient Tie-Breaking:** Text-only LLM for semantic equivalence instead of multimodal vision is faster, cheaper, and less prone to hallucination.
- **Agent Transparency:** Structured JSON in `long_term_memory` provides ground truth for informed agent decisions.
- **Tiered Escalation Preserved:** Vision (Stagehand) kept as last resort while demoted from primary verification path.

### Concerns

- **HIGH -- Context/Token Bloat.** Per-field review rows in `long_term_memory` for every interaction can quickly exhaust token budget in complex multi-page ATS flows. Risk: agent loses earlier conversation history or becomes sluggish.

- **MEDIUM -- Python/TS Logic Drift.** Porting normalization and fuzzy-match rules from VerificationEngine.ts to Python is prone to subtle parity drift. A field might pass verification in staging (TS) but fail in production (Python).

- **MEDIUM -- Module-Level State Persistence.** Plan mentions addressing `_COMPLETED_SCOPED_FILLS` but doesn't define the replacement.

- **MEDIUM -- Oracle Shadow DOM / Custom Widgets.** Oracle HCM components often don't update standard `.value` property. Strict deterministic readback will trigger high volume of `readback_mismatch` on platforms Hand-X is specifically built to handle.

### Suggestions

1. **Token Budgeting:** Provide "Summary + Delta" JSON. Include all `failed` and `readback_mismatch` fields, but only count/summary for `verified` fields.
2. **Shared Test Oracle:** JSON-based "Golden Rules" file with normalization test cases, driving unit tests in both TS and Python for 100% parity.
3. **Platform-Specific Read Probes:** Allow VerificationEngine to accept a `read_probe` lambda from platform handler.
4. **Explicit State Injection:** Phase 3 `DomHandPipeline` class instantiated with `session_context` to kill module-level globals.
5. **PII Masking:** Truncate/mask `actual_read` values before sending to LLM for semantic equivalence.

---

## Claude Agent Review (Code Reviewer)

**Risk Assessment: MEDIUM**

### Summary

The plan correctly diagnoses the central defect: `_attempt_domhand_fill_with_retry_cap` silently promotes `readback_unverified` (confidence 0.55) into `success: True`. The four-phase approach is ordered correctly for incremental value. However, the plan has a significant blind spot around `long_term_memory` token accumulation, underspecifies the taxonomy for several control types, and Phase 3 carries outsized risk.

### Strengths

- **Correct root cause identification.** Precisely identifies the `FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED = 0.55` path at fill_verify.py:36 and :456 as the core defect.
- **Phase ordering maximizes incremental value.** Phase 1 can ship independently and immediately fixes the agent visibility gap.
- **Text-only semantic equivalence is the right abstraction.** Current `llm_verify_field_value` takes screenshots and sends multimodal messages — non-deterministic, expensive, and fragile.
- **Preserves existing escalation tiers.** Does not propose removing Stagehand or vision escalation.
- **Reconciliation against live DOM preserved.** Extends existing `_reconcile_fill_results_against_live_dom` rather than replacing.

### Concerns

- **HIGH -- Taxonomy incomplete for several control types.**
  - `already_matched` (field_already_matches returns early) should be distinct `already_correct`
  - `retry_capped` is distinct from `execution_failed` ("gave up" vs "current attempt failed")
  - `stagehand_rescued` / `llm_rescued` set confidence 0.6/0.8 but report success=True — not DOM-verified
  - Multi-select partial match (3 of 5 skills) maps to `readback_mismatch` even when mostly correct

- **HIGH -- `long_term_memory` JSON will cause context bloat without hard cap.** Every step's `long_term_memory` concatenated into `action_results` via message_manager. A 40-field Oracle page produces 3-5KB per invocation. Across 4-page flow with 2-3 rounds per page = 25-40KB JSON persisted. Must specify: (a) hard 1500-char cap, (b) tiered truncation (totals always, per-field rows only for failures), (c) replace-vs-append policy.

- **HIGH -- Prior review finding on module-level state not addressed.** Plan doesn't mention the fix already applied at domhand_fill.py:2710 or flag remaining module-level state risks.

- **MEDIUM -- Grouped date fields don't fit readback model.** Oracle segmented dates use `component_field_ids`. Parent readback returns empty while child segments are correctly filled. Must aggregate child readbacks.

- **MEDIUM -- File upload fields silently excluded.** File inputs never go through `_attempt_domhand_fill_with_retry_cap`. Review JSON will produce confusing results if file fields appear with no review status.

- **MEDIUM -- Phase 2 may not need LLM for 80% of cases.** Expand `_field_value_matches_expected` with country name, phone format, date format, state abbreviation normalization first. LLM only after deterministic expansions fail.

- **MEDIUM -- Agent-facing JSON will contain PII.** `FillFieldResult.value_set` holds actual filled values (name, email, phone, SSN). Must be omitted or truncated.

- **LOW -- Phase 3 adds marginal value for significant risk.** Testability achievable by extracting just verification logic without full pipeline restructure.

- **LOW -- Oracle `cx-select` readback will report more failures.** Need Oracle-specific `widget_opaque` review status.

### Suggestions

1. Expand taxonomy to 5 values: `verified`, `execution_failed`, `readback_mismatch`, `already_correct`, `readback_opaque`.
2. Hard 1500-char token budget for `long_term_memory` JSON with tiered truncation.
3. Add `dom_read_actual: str` and `review_status: str` to FillFieldResult in Phase 1.
4. Split Phase 2 into 2a (deterministic normalization) and 2b (LLM semantic equivalence).
5. Defer Phase 3 entirely. Extract `verification_engine.py` only.
6. Redact PII: replace `value_set` with `value_length` + `value_type` in agent-facing JSON.
7. Add explicit `actor` values for escalation paths (stagehand, llm_escalation).
8. Audit sibling actions for P3 directive consistency with new review contract.

---

## Consensus Summary

### Issues Raised by All 3 Reviewers

| # | Concern | Codex | Gemini | Claude | Priority |
|---|---------|-------|--------|--------|----------|
| 1 | **`verified: bool` too lossy / taxonomy incomplete** — need multi-valued review status distinguishing verified, mismatch, unreadable/opaque, and already-correct states | HIGH | Implicit in taxonomy praise | HIGH (5 missing states) | **Must-fix** |
| 2 | **`long_term_memory` token bloat + PII** — per-field JSON will exhaust token budget and leak sensitive data | MEDIUM | HIGH | HIGH (25-40KB math) | **Must-fix** |
| 3 | **Deterministic normalization before LLM** — exhaust country/phone/date/state rules before adding LLM equivalence | MEDIUM | Implicit in cost praise | MEDIUM (80% covered) | **Must-fix** |
| 4 | **Oracle HCM will over-report failures** — cx-select/shadow widgets produce empty/opaque readback, not true mismatches | MEDIUM | MEDIUM | LOW (opaque status) | **Should-fix** |

### Issues Raised by 2 Reviewers

| # | Concern | Raised by | Priority |
|---|---------|-----------|----------|
| 5 | **`domhand_fill` and `domhand_assess_state` must share review engine** — split-brain verification risk | Codex (HIGH) + Claude (implicit) | **Must-fix** |
| 6 | **Prompts must be updated in same phase** — current prompts contradict new review contract | Codex (HIGH) + Claude (P3 audit) | **Should-fix** |
| 7 | **Phase 3 refactor risk outweighs value** — extract verification_engine.py only, defer pipeline restructure | Gemini (implicit) + Claude (LOW) | **Should-fix** |
| 8 | **Python/TS parity drift** — need shared golden test cases | Gemini (MEDIUM) + Claude (golden strings) | **Should-fix** |

### Unique Findings (single reviewer)

| # | Concern | Reviewer | Priority |
|---|---------|----------|----------|
| 9 | **Two-axis model: `execution_status` x `review_status`** — the shortest correct model | Codex | **Should-adopt** |
| 10 | **Grouped date fields need child-aggregate verification** | Claude | **Should-fix** |
| 11 | **File upload fields need explicit exclusion or `not_applicable` status** | Claude | **Nice-to-have** |
| 12 | **Field-family typed review payloads** (scalar, token-set, composite, boolean, file) | Codex | **Should-fix** |

### Agreed Strengths (all 3 reviewers)

- **Phase ordering correct** — Phase 1 is highest leverage, can ship independently
- **Core premise correct** — verification must be deterministic DOM/CDP, not screenshots
- **Vision demotion right** — multimodal verification should be rare fallback, not primary path
- **Answer synthesis correctly scoped** — LLM for match/process layer only, not verification
- **Phase 3 optional is pragmatic** — brownfield rewrite risk acknowledged

### Risk Assessment Summary

| Reviewer | Risk | Key Reasoning |
|----------|------|---------------|
| Codex (GPT-5.4) | **MEDIUM** | Review truth must be shared AND model-visible; taxonomy too lossy |
| Gemini | **MEDIUM** | Token bloat risk; Python/TS parity drift |
| Claude Agent | **MEDIUM** | Taxonomy incomplete (5 missing states); grouped date/file edge cases |

**Weighted consensus: MEDIUM.** All three reviewers agree the direction is correct but the contract boundary is underspecified. The top action items: (1) adopt two-axis `execution_status` x `review_status` model, (2) hard token cap on agent-visible JSON with PII redaction, (3) unify verification logic between `domhand_fill` and `domhand_assess_state` in Phase 1, (4) exhaust deterministic normalization before LLM equivalence, (5) add Oracle-specific `readback_opaque` status to prevent false-negative flood.
