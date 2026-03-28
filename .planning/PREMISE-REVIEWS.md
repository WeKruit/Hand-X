---
phase: Premise Handoff
reviewers: [codex-gpt54, gemini, claude-opus-code-reviewer]
reviewed_at: 2026-03-26T21:55:00Z
plans_reviewed: [5 Architectural Premises for DomHand agent loop]
---

# Cross-AI Plan Review -- Premise Handoff

## Codex Review (GPT-5.4)

**Risk Assessment: HIGH**

### Summary

Directionally, the refactor is good: it reduces imperative leakage from DomHand, makes browser-use own the decision loop again, and adds much better explicit premise coverage than before. But the premises are not yet enforced as claimed. P3 is already violated by at least one live action, P1 can be broken by stale "same-page" guards in the service loop, and P2 remains partly declarative because the refill state machine still hinges on `requires_assess_checkpoint` while `domhand_fill`'s richer state is hidden from the model.

Tests verified: `uv run pytest -q tests/unit/test_premises.py` (60 passed) and `uv run pytest -q tests/ci/test_action_loop_detection.py` (51 passed).

### Findings / Concerns

- **HIGH -- P3 violated by domhand_expand.py**: `domhand_expand.py` lines 385 and 396 still emit directive `extracted_content` telling the agent to call `domhand_fill` ("Now call domhand_fill to fill the new entry fields."). The AST test only inspects string literals passed directly into `ActionResult(...)`, so it misses variable-built messages. The "60 passed" suite does not prove P3 as a structural invariant.

- **HIGH -- Service guards not page-scoped**: `_same_page_fill_checkpoint_decision` and `_same_page_advance_decision` do not accept or verify current page identity (`service.py:286`, `service.py:249`); they are invoked unconditionally before action execution (`service.py:1426`). Meanwhile `domhand_fill` persists prior-page state (`domhand_fill.py:3765`). Under the new flow "fill -> visual review -> advance", stale guards can directly violate P1.

- **MEDIUM -- P2 contradiction goes beyond naming**: Refill blocking is still keyed off `requires_assess_checkpoint` (`domhand_fill.py:1754`, `service.py:293`), and `domhand_assess_state` is still what consumes that checkpoint (`domhand_assess_state.py:1030`). The new "inline state" from `domhand_fill` lives only in metadata (`domhand_fill.py:3784`) but DomHand results are suppressed from model-visible read state (`message_manager/service.py:34`, `:355`). "Fill returns inline state; assess is optional" is not yet the operative runtime contract.

- **MEDIUM -- Blocker semantics inconsistent**: `domhand_assess_state` treats `opaque_fields` as blocking for `advance_allowed` (`domhand_assess_state.py:1511`), but `_blocker_guard_decision` only treats unresolved required, optional validation, and visible errors as active hard blockers (`service.py:193`). In an opaque-only state, broad refill can slip past the service guard after the assess checkpoint is consumed, weakening P4.

- **MEDIUM -- P1 prompt contradiction**: System prompt says `domhand_fill` is "ALWAYS your FIRST action on any form page" (`prompts.py:520`), while task prompt says to upload resume first and then call `domhand_fill` (`prompts.py:1000`). The corresponding test only checks for presence of `"domhand_fill"` plus "first action" wording anywhere (`test_premises.py:507`), so it passes despite the contradiction.

### Strengths

- Removing assess guidance injection in `message_manager/service.py:205` is the right architectural move -- narrows a hidden instruction channel.
- Factual rewrites in `service.py:1430` and `domhand_fill.py:3743` materially reduce imperative coupling between tool text and agent planning.
- P4 matrix tests in `test_premises.py:334` are useful. They make the intended broad-vs-scoped distinction much clearer than before.
- Prompt rewrite in `prompts.py:540` and `:1006` consistently centers visual self-review, coherent with P5.

### Suggestions

1. **Make service guards actually same-page.** Pass current page URL/context key into `_same_page_fill_checkpoint_decision` and `_same_page_advance_decision`, or clear stale `_gh_last_domhand_fill` / `_gh_last_application_state` on page transition before guard evaluation.
2. **Replace `requires_assess_checkpoint`** with a declarative page-state bit like `broad_fill_completed_for_page`, and stop letting `domhand_assess_state` mutate refill permission. That would make P2 and P4 coherent.
3. **Extend P3 enforcement to runtime outputs** from every DomHand action, not just literal AST fragments. At minimum, add a regression for `domhand_expand.py:381` and scan `error=` too, since errors are model-visible.
4. **Unify blocker classification** behind one helper shared by `domhand_assess_state` and the service guards so `opaque_count` and similar signals cannot drift.
5. **Move scoped-fill dedup off module globals** in `domhand_fill.py:320` onto `browser_session`; the current global state is a cross-session contamination risk.

---

## Gemini Review

**Risk Assessment: LOW-MEDIUM**

### Summary

The implementation successfully transitions the agent loop from a directive-driven system to an observational one. The core of this change lies in Premise 3 (No Directives), which has been rigorously applied to both action outputs and agent-loop guards. The new Premise 4 (One Broad Fill) guard logic provides a necessary throttle on expensive/repetitive DOM actions while allowing the precision recovery required for complex forms. The inclusion of an AST-based structural invariant test suite (`test_premises.py`) is an elite engineering choice that ensures these architectural boundaries cannot be accidentally regressed by future string changes.

### Concerns

- **HIGH -- State Leakage Risk**: `_COMPLETED_SCOPED_FILLS` is implemented as a module-level global dictionary in `domhand_fill.py`. If multiple agent sessions share the same Python process, their scoped fill histories will collide and overwrite each other. This breaks multi-tenancy/multi-session reliability.

- **MEDIUM -- P1 Runtime Enforcement Gap**: While the prompt mandates "DomHand first," there is no runtime guard in `Agent._execute_actions` to stop an agent from performing a `click` or `input` as its very first action on a new page. Relies entirely on LLM prompt adherence.

- **LOW -- P3 AST Scan Blindness**: The AST scanner currently looks for string literals. Dynamically constructed strings (e.g., `f"Next action: {calc_next()}"`) or strings passed as variables will escape enforcement. Most current outputs are literals, but this is a technical gap.

- **LOW -- P2 Conceptual Contradiction**: The code still uses `requires_assess_checkpoint` as the primary gate for re-fills. This tethers P4 (guarding re-fills) to the legacy "Assess Step" concept, even though P2 claims assessment is now optional. Primarily a naming/conceptual mismatch.

### Strengths

- **P3 Enforcement Rigor**: Using AST parsing to scan all `ActionResult` return paths for "directive" language is a robust way to enforce the "informational-only" mandate. Treats architecture as a testable invariant.
- **Context Efficiency**: Gutting `_build_assess_guidance_note` and clearing `read_state` on page transitions drastically reduces LLM context noise, preventing stale "previous page" data from confusing the agent.
- **Scoped Guard Logic**: The guard system in `domhand_fill.py` correctly distinguishes between "broad refills" (blocked) and "targeted/scoped fills" (allowed). Critical for P4.
- **Oracle HCM Specialization**: The addition of `oracle_combobox` strategy and platform-specific dispatch (P1) demonstrates the new architecture still supports deep, site-specific workarounds.

### Suggestions

1. **Move Dedup to Session**: Relocate `_COMPLETED_SCOPED_FILLS` to `browser_session._gh_completed_scoped_fills` to ensure thread-safety and session isolation.
2. **Add P1 Loop Guard**: Add a simple "first action" check in `Agent.step()` that issues a factual warning if the first action on a new `page_identity` is not `domhand_fill`.
3. **Expand AST Scanner**: Update `test_premises.py` to also scan the `error=` keyword argument in `ActionResult` calls, as factual errors should also be directive-free.
4. **Clean up P2 Naming**: Rename the `requires_assess_checkpoint` flag to `broad_fill_completed`. This aligns with the "one-shot" nature of P4 and removes the implication that an `assess_state` call is the intended next step.

---

## Claude Agent Review (Code Reviewer)

**Risk Assessment: MODERATE (behavioral regression) + MEDIUM (data integrity)**

### Summary

The core thesis is sound: DomHand should be an informational tool that reports what it did, while the agent owns decision-making via visual page observation. The implementation is largely successful at removing directive language from agent-facing output, gutting the guidance injection system, and tightening the guard system. However, there are several issues: a mixed-indentation problem in `service.py`, an incomplete P3 sweep of sibling actions, `recommended_next_action` metadata as a shadow directive channel, and an Oracle combobox fallback that can silently inject wrong values.

The changeset also bundles two orthogonal features (Oracle HCM combobox support, LLM escalation model swap to Gemini Flash) that should be evaluated separately.

### Concerns

**CRITICAL:**

- **(C1) Mixed tabs/spaces in `browser_use/agent/service.py` dict literals** (lines 210-211, 239-243, 280-281, 310-311). New lines use hard tabs while surrounding code uses spaces. Python parses it, but `ruff format` will either normalize or reject it. Ticking time bomb for CI.

**HIGH:**

- **(C2) Incomplete P3 sweep: `domhand_select.py` and `domhand_interact_control.py`** still emit directives in `error` strings. `domhand_interact_control.py:655` says "Do not repeat the same DomHand strategy". `domhand_select.py:1196` says "STOP -- do NOT retry domhand_select". These are `ActionResult.error`, visible to the agent. P3 violations.

- **(C3) `recommended_next_action` still present in metadata** across 30+ locations in `domhand_select.py`, `domhand_interact_control.py`, `domhand_record_expected_value.py`, and the cleaned `service.py`. While metadata isn't directly `extracted_content`, it's accessible and creates a shadow directive channel.

- **(C4) `domhand_fill` advance guard still has `"advance_or_manual_browser_decision"`** in metadata (`domhand_fill.py:2752`). This is the old directive-style value, inconsistent with the new `"review_page_visually"` convention.

**MEDIUM:**

- **(C5) Module-level mutable global state** for scoped dedup is not thread-safe. `_COMPLETED_SCOPED_FILLS` and `_COMPLETED_SCOPED_PAGE` at `domhand_fill.py:320-321`. Correct for single-job but a latent concurrency bug.

- **(C6) `structured_summary_agent` lost diagnostic fields.** Agent-facing JSON went from containing `dom_failure_count`, `skipped_count`, `unfilled_count`, `best_effort_guess_count`, `unresolved_required_fields`, `failed_fields`, etc. to only `filled_count` and `already_filled_count`. While P3-pure, this removes genuinely informational data. If the agent can't visually detect a subtle below-fold error, it has zero signal. Consider keeping `dom_failure_count` and `unfilled_count` as pure counts (facts, not directives).

- **(C7) LLM escalation model swap bundled.** `fill_llm_escalation.py` now uses `_get_escalation_model()` reading `settings.domhand_model`. The swap from direct Anthropic client to LangChain `get_chat_model()` changes error surface. LangChain wraps exceptions differently.

- **(C8) Oracle combobox JS selects first visible option as fallback** when no match found. `fill_executor.py` in `_ORACLE_COMBOBOX_CLICK_BEST_OPTION_JS`: if user profile says "Computer Science" but dropdown only shows "Accounting", "Biology", "Chemistry" -- it selects "Accounting". Wrong value injection is worse than empty field.

**LOW:**

- **(C9) `_build_assess_guidance_note` is dead code.** Method body is just `return ''`. Consider removing entirely.

- **(C10) Scoped dedup guard tests manipulate globals directly** rather than going through `domhand_fill`. Guard integration path is untested.

- **(C11) `fill_verify.py` FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED** change means DomHand reports `success=True` when executor succeeded but DOM readback never matched. Combined with (C8), Oracle combobox could fill wrong value but report success with confidence 0.55.

### Premise Coherence Table

| Premise | Coherent? | Enforced? | Notes |
|---------|-----------|-----------|-------|
| P1 (DomHand first on every new page) | Yes | Via prompts only | Prompt says "ALWAYS FIRST action" but also "upload resume FIRST" |
| P2 (Fill returns inline state; assess is optional) | Yes | Mostly | `structured_summary_agent` lost diagnostic counters; assess demoted to #7 |
| P3 (Output is purely informational) | Yes | Partial | `domhand_fill` and `assess_state` clean; `select`/`interact_control` still emit directives; metadata `recommended_next_action` is shadow channel |
| P4 (One broad fill; targeted for recovery) | Yes | Yes | Guard system correct; scoped dedup works |
| P5 (Agent self-reviews via vision) | Yes | Yes | Prompt rewrites consistently center vision |

### Strengths

- Clean `_build_assess_guidance_note` gutting (P3) -- single most impactful change.
- Agent-facing summary rewrite in `domhand_fill.py` -- textbook P3 compliance.
- Prompt hierarchy rewrite (P1, P5) -- demoting assess to #7 is coherent.
- Guard message cleanup (P3) -- factual signals without embedded directives.
- Scoped dedup guard (P4) -- correct `focus_fields` bypass for recovery.
- Test assertion updates faithful to new semantics.

### Suggestions

1. **Run `ruff format` on `browser_use/agent/service.py`** to fix mixed tabs/spaces (C1).
2. **Apply P3 treatment to `domhand_select.py` and `domhand_interact_control.py`** (C2).
3. **Remove first-option fallback in Oracle combobox JS** -- return `{clicked: false, reason: 'no_match'}` instead (C8).
4. **Keep `dom_failure_count` and `unfilled_count` in `structured_summary_agent`** as bare integers -- facts, not directives (C6).
5. **Move `_COMPLETED_SCOPED_FILLS` to `browser_session`** (C5).
6. **Remove or fully gut `_build_assess_guidance_note`** (C9).

---

## Consensus Summary

### Issues Raised by All 3 Reviewers

| # | Concern | Codex | Gemini | Claude | Priority |
|---|---------|-------|--------|--------|----------|
| 1 | **Module-level global state** (`_COMPLETED_SCOPED_FILLS`) cross-session risk | Suggestion #5 | HIGH concern #1 | C5 MEDIUM | **Must-fix** |
| 2 | **P3 incomplete** -- sibling actions (`domhand_select`, `domhand_interact_control`, `domhand_expand`) still emit directives | HIGH (found expand) | LOW (acknowledged gap) | C2 HIGH (found select+interact) | **Must-fix** |
| 3 | **P2 naming/semantic contradiction** -- `requires_assess_checkpoint` implies assess required | MEDIUM | LOW | Part of coherence analysis | **Should-fix** |
| 4 | **`error=` field not covered** by P3 enforcement | Suggestion #3 | Suggestion #3 | C2 (in error strings) | **Should-fix** |

### Issues Raised by 2 Reviewers

| # | Concern | Raised by | Priority |
|---|---------|-----------|----------|
| 5 | **P1 no runtime enforcement** -- prompt-only compliance | Codex (MEDIUM) + Gemini (MEDIUM) | **Should-fix** |
| 6 | **Service guards not page-scoped** -- stale state blocks first fill on new page | Codex (HIGH) + Claude (implicit in C4 metadata inconsistency) | **Should-fix** |
| 7 | **Blocker semantics inconsistent** -- opaque_fields treated differently between assess and service | Codex (MEDIUM) + Claude (partially in C11) | **Nice-to-have** |

### Unique Findings (single reviewer)

| # | Concern | Reviewer | Priority |
|---|---------|----------|----------|
| 8 | **Mixed tabs/spaces in service.py** -- CI time bomb | Claude (CRITICAL) | **Must-fix** |
| 9 | **`recommended_next_action` metadata** -- 30+ shadow directive channels | Claude (HIGH) | **Should-fix** |
| 10 | **Oracle combobox first-option fallback** -- wrong value injection | Claude (MEDIUM) | **Should-fix** |
| 11 | **`structured_summary_agent` lost diagnostic counters** -- agent effectiveness regression | Claude (MEDIUM) | **Should-fix** |
| 12 | **P1 prompt contradiction** -- "ALWAYS first" vs "upload resume FIRST" | Codex (MEDIUM) | **Should-fix** |
| 13 | **LLM escalation model swap bundled** -- different error surface | Claude (MEDIUM) | **Nice-to-have** |

### Agreed Strengths (all 3 reviewers)

- **AST-based structural invariant tests** -- treats architecture as testable invariants
- **`_build_assess_guidance_note` removal** -- correctly narrows hidden instruction channel
- **Scoped guard logic (P4)** -- broad-vs-targeted distinction correctly implemented
- **Visual self-review (P5)** -- prompt rewrites coherently center vision-based decisions
- **Agent-facing summary rewrite** -- textbook factual output replacing directive text

### Risk Assessment Summary

| Reviewer | Risk | Key Reasoning |
|----------|------|---------------|
| Codex (GPT-5.4) | **HIGH** | P3 has concrete live violation; service guards can mis-scope pages |
| Gemini | **LOW-MEDIUM** | Sound architecture; only global state is high-risk |
| Claude Agent | **MODERATE** | Behavioral regression from lost diagnostic data; partial P3 sweep |

**Weighted consensus: MEDIUM-HIGH.** The architectural direction is unanimously praised. The risk is in incomplete enforcement: P3 has 3+ known live violations across sibling actions, the service guard scoping can break P1 on page transitions, and the lost diagnostic counters may degrade agent effectiveness. The must-fix items (global state, P3 sibling sweep, tabs/spaces) are bounded and straightforward.
