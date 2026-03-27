# Deterministic form observation (research)

**Question:** How can the agent *know* what DomHand (or the page) actually contains, without guessing from screenshots or sparse DOM indices?

**Short answer:** Treat **vision and LLM interpretation of screenshots as non-deterministic**. Treat **browser-use’s indexed `llm_representation()` as a lossy, reorderable view** of the real DOM. The only **repeatable** signal in this codebase is the same **CDP/DOM extraction pipeline** DomHand already uses: `extract_visible_form_fields` + value/validation reads (`fill_verify`, `_field_has_effective_value`, assess_state internals).

---

## What is *not* deterministic

| Channel | Why it fails for “did we fill?” |
|--------|----------------------------------|
| **Screenshot + vision model** | Stochastic decoding, OCR/overlay ambiguity, compression, timing (spinner vs settled UI). |
| **Agent “memory” / eval text** | Model confabulation; not grounded in DOM. |
| **browser-use `get_browser_state_summary` → `dom_state.llm_representation()`** | Paint-order filtering, max depth on text, truncated labels, **index churn** after SPA updates. Oracle HCM often keeps the same URL while the tree reshapes. |
| **Suppressed / missing tool text in history** | If DomHand factual summaries never reach `action_results`, the planner has **only** the lossy browser state (fixed separately via `long_term_memory` mirroring factual `extracted_content`). |

---

## What *is* deterministic (within “our extractor agrees with the page”)

1. **`extract_visible_form_fields(page)`** (GhostHands)  
   Produces structured `FormField` rows: stable keys, labels, types, and **best-effort `current_value`** from the same JS/CDP logic DomHand uses to fill.

2. **Post-fill verification** (`ghosthands/dom/fill_verify.py`, executor reads)  
   Re-reads the control after actions; tracks retry state. This is **machine-checkable**, not narrative.

3. **`domhand_assess_state`**  
   Already aggregates extracted fields into `application_state` (required unresolved, errors, advance hints). **Structured** (`model_dump_json` in metadata) — but the planner historically did not see it when `read_state` was suppressed without a `long_term_memory` bridge.

**Caveat:** “Deterministic” here means **deterministic given our extraction code and the live DOM**. Oracle custom widgets can still lie (e.g. displayed chip vs underlying `value`) until we add **widget-specific read probes** — that’s a *coverage* problem, not solved by screenshots.

---

## Recommended contract (make “observation” a product surface)

**Premise 5 (“review the page”) should be split:**

1. **Human / HITL:** screenshots and a real browser are appropriate.
2. **Agent planning loop:** must receive a **canonical `FormObservationSnapshot`** after any broad `domhand_fill` (and optionally after navigation within the same `page_context_key`), not rely on the model “seeing” pixels or index soup.

### Snapshot contents (minimal)

- `page_context_key`, `url`, `extracted_at` (monotonic step id).
- Per visible field (cap N rows + hash of full set for tests):
  - `field_key`, `field_id`, `label`, `field_type`
  - `effective_value` (normalized string) and/or `has_value: bool`
  - `required`, `has_validation_error` (boolean from same probes as fill_verify where possible)

**Source of truth:** one function, e.g. `build_form_observation_snapshot(page) -> dict`, implemented **only** on top of `extract_visible_form_fields` + shared validation helpers — **no** new LLM calls.

### Where it goes

- **Append to planner context** as compact JSON inside `long_term_memory` or a dedicated non-suppressed block (policy decision: keep `<read_state>` small, but **never** drop the snapshot).
- **Log** the same JSON at `INFO` for humans tailing logs (deterministic diff across steps).

### Tests

- Golden **fixture HTML** (toy app + Oracle fixture) asserting snapshot matches known values after scripted fills.
- Regression: after `domhand_fill`, **snapshot required_fields satisfied** implies agent must not emit duplicate `input` for those labels (agent policy / guard — optional follow-up).

---

## Why screenshots in logs still “failed”

Logs save PNGs; the **next model step** may not attach that image to the **exact** message where it decides to re-type. Even when attached, **vision is not a verifier** — it’s a weak sampler. For automation correctness, **treat screenshots as debug artifacts**, not ground truth.

---

## Next implementation slice (when ready)

1. Implement `build_form_observation_snapshot` next to `extract_visible_form_fields` consumers.
2. Call it at end of `domhand_fill` success path; merge **trimmed** JSON into `long_term_memory` (and keep `agent_summary` prose for humans).
3. Optionally expose `domhand_observe_fields` tool that only returns snapshot (no fill) for mid-step debugging.
4. Extend Oracle platform module with **explicit read strategies** for known-bad widgets (address cluster, school combobox) where `current_value` is unreliable.

---

## GHOST-HANDS `staging` / v3 verification — what we could (not) inspect

**Workspace note:** The legacy **GHOST-HANDS** repository is **not** mounted under `VALET & GH/` in this environment, and **Hand-X** has **no `staging` branch** in local `git branch -a` output checked during research. So we **cannot** diff the old “verification engine v3” source line-by-line here.

**What Hand-X already carries from that lineage (traceable in git / comments):**

| Signal | Location |
|--------|----------|
| **Tiered escalation** (DOM → LLM verify → LLM fill → agent vision) | `ghosthands/dom/fill_llm_escalation.py` docstring: *“Graduated cost model inspired by GHOST-HANDS v3 tiers”* |
| **Dropdown / option architecture port** | `git log` message: *“port GHOST-HANDS dropdown architecture”* (`98d1bba0a`) |
| **Per-field observable verify** (poll readback vs desired) | `ghosthands/dom/fill_verify.py` — `_verify_fill_observable()` |
| **Post-fill “success but readback disagrees”** | Same file — `FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED` (0.55): executor said OK but DOM read path never matched within the poll window → **still counts success** to avoid blocking the agent. This is a **major noise source**: logs look like fill worked, assess/agent disagree. |

**What is *not* in Hand-X today (vs your “before/after interactive diff” idea):**

- No **full-page** snapshot of “all interactive elements” taken **before** `domhand_fill` and **after**, then differenced.
- No **stable identity** diff keyed off `backendNodeId` / CDP `DOMSnapshot` — DomHand uses **`field_id` / `get_stable_field_key`** and in-page reads instead.

**Stagehand:** `observe()` is for **discovery** (model + heuristics), not a deterministic verifier. `fill_verify._stagehand_escalate_fill` uses `act()` then **DOM re-verify** — still **per-field**, and Stagehand itself consumes an LLM.

---

## Minimum-LLM “before / after” aligned with DomHand (recommended)

**Goal:** Replace *“blindly filled or not”* with a **three-valued** outcome per scoped field: **`verified` | `failed` | `inconclusive`**, where **LLM only runs on `inconclusive`** (and cap calls).

1. **Before** touching a field (or before a fill round for the fillable set): capture `Snapshot A`:  
   `dict[field_key, ObservationRecord]` where `ObservationRecord` includes at least:  
   `value_text`, `checked` / `selected` as applicable, `has_validation_error`, optional `opaque: bool` (we already have `_field_current_value_is_opaque` patterns in assess).

2. **After** fill (or end of round): capture `Snapshot B` with the **same** readers (same JS/CDP path — **0 LLM**).

3. **Diff (deterministic):**  
   - If `B[k] == A[k]` for a field we intended to set → **failed** (no change).  
   - If `B[k]` matches normalized expected (existing `_field_value_matches_expected`) and no validation error → **verified**.  
   - If read is empty / opaque / widget-specific unknown → **inconclusive** (do **not** claim filled; optionally one bounded LLM or platform-specific probe — not vision-first).

4. **Stop using** “success + `FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED`” as a silent happy path in metrics/agent summaries; surface **`inconclusive_readback`** in structured metadata so the planner doesn’t hallucinate a filled form.

5. **Scope:** Diff **DomHand-known keys** for the current page/section, not browser-use’s index list — avoids SPA index churn and keeps cost **O(fields we touch)** not **O(all interactives)**.

**LLM budget:** 0 calls on **verified** / **failed** paths; optional 1 small call only when **`inconclusive`** after N polls (today’s `llm_verify_field_value` is already optional — tighten when it fires).

---

## References (in-repo)

- `browser_use/browser/session.py` — `get_browser_state_summary` (cached DOM + screenshot path).
- `browser_use/mcp/server.py` — `_get_browser_state` (shows how interactive list is truncated for tools).
- `browser_use/agent/message_manager/service.py` — `_READ_STATE_SUPPRESSED_TOOLS`, `long_term_memory` → `action_results`.
- `ghosthands/dom/` — `extract_visible_form_fields`, `fill_verify.py`.
- `ghosthands/actions/domhand_assess_state.py` — structured `application_state`.
