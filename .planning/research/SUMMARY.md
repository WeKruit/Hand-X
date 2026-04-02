# Project Research Summary

**Project:** Hand-X Observation Layer v2.0 Rebuild
**Domain:** Browser automation observation for ATS job application form filling
**Researched:** 2026-04-02
**Confidence:** HIGH

## Executive Summary

The Hand-X observation layer rebuild is a brownfield replacement of the DOM extraction pipeline that feeds the form-filling agent. The action layer (fill executor, verification engine, browser scripts) stays unchanged; only observation is rebuilt. Industry consensus from WebArena, Stagehand, rtrvr.ai, and browser-use converges on one approach: **accessibility tree as primary semantic signal, DOM for structural anchoring, vision only as escalation**. The existing browser-use library already implements the expensive CDP fusion (DOM + a11y tree + DOMSnapshot in parallel), so the rebuild should layer on top of it rather than building a parallel pipeline.

The recommended architecture is a three-layer synchronous pipeline: (1) Raw Extractor that wraps existing CDP/JS extraction into a clean `RawPageSnapshot`, (2) Semantic Interpreter that classifies field types, resolves labels, and groups related fields in Python (not JS), and (3) State Tracker that diffs observations across agent steps using MutationObserver for dirty-field detection. This architecture produces deterministic, LLM-free observation that the fill LLM consumes as structured field metadata -- solving the documented number one problem of "overwhelming the LLM with raw DOM context causing hallucinations."

The critical risk is **regression on platforms that currently work**. Six ATS platforms (Workday, Greenhouse, Lever, Oracle, SmartRecruiters, Phenom) each have unique widget patterns baked into the current extractor. The rebuild must pass a platform parity matrix before replacing the old extractor for any platform. Secondary risks are a11y tree gaps for custom widgets (button groups, Workday segmented dates) and performance of `getFullAXTree` on large Workday pages. Both must be validated in a Phase 1 spike before committing to the architecture.

## Key Findings

### Recommended Stack

The stack is almost entirely in-house -- no new dependencies required. The rebuild leverages existing CDP APIs, browser-use internals, and Pydantic models.

**Core technologies:**
- **CDP `Accessibility.getFullAXTree` + `DOM.getDocument` + `DOMSnapshot.captureSnapshot`**: Three-tree fusion for semantic roles, DOM structure, and visual positioning -- already implemented in browser-use's `DomService`
- **MutationObserver (Web API)**: Event-driven dirty-field detection between agent steps -- attribute-filtered, debounced, not always-on
- **Pydantic v2**: Schema definition and validation for `SemanticField`, `PageObservation`, `ObservationState` models -- already the codebase standard
- **Gemini 2.5 Flash**: Strategic per-page screenshots at ~$0.0001/image for vision escalation when DOM observation has low confidence -- 20-60x cheaper than Claude/GPT-4o for this use case
- **Playwright a11y APIs (`ariaSnapshot`)**: For golden-file test assertions only, not production observation (lacks backendNodeId and bounding boxes)

**Critical version requirement:** Chrome 130+ for full CDP Accessibility domain support.

**Cost per observation:** ~$0 for DOM+a11y extraction (pure CDP, no LLM). ~$0.0004 per 5-page application for strategic screenshots with Gemini Flash.

### Expected Features

**Must have (table stakes):**
- Deterministic field discovery with stable ordering (same page = same output, always)
- Label resolution with confidence tracking (aria-labelledby > aria-label > label[for] > sibling heuristic > placeholder > name)
- Canonical widget taxonomy mapping platform-specific widgets to canonical types (text, select, radio_group, checkbox_group, date, file, toggle, button_group)
- Structured observation output with diff support ("what changed" vs full dump)
- DOM stability waiting (MutationObserver-based settle detection replacing hardcoded asyncio.sleep)
- Loading state detection (spinners, skeleton screens, "Loading..." text)
- Shadow DOM traversal, visibility detection, disabled state detection, option enumeration
- Fill verification via post-action readback (existing `verification_engine.py` -- keep it)

**Should have (differentiators):**
- Observation diff for LLM context compression -- report only changed fields between steps
- Field completeness scoring per page ("17/20 filled, 2 required empty")
- Confidence scoring per field based on label source quality
- Semantic section classification (map heading variations to canonical categories)
- Platform detection heuristic with widget fingerprint library
- Conditional reveal detection via MutationObserver (proactive, not just reactive rescan)

**Defer to v2.1+:**
- Generic repeater section detection (current platform-specific approach works)
- Nested group hierarchy (requires model change; flat field list is functional)
- Question intent detection (LLM-dependent; can layer on after core observation is solid)
- Proactive MutationObserver for conditional reveals (reactive rescan works, just slower)

**Anti-features (deliberately avoid):**
- Per-action screenshots (cost spiral: $0.75-7.20 per application)
- Full DOM serialization to LLM (documented cause of hallucinations)
- Platform-specific extraction code paths (use declarative pattern registry instead)
- LLM-based field type classification (must be deterministic and instant)
- Always-on MutationObserver (performance killer on Workday/Oracle)

### Architecture Approach

**Three-layer synchronous pipeline** building on top of browser-use's existing `DomService`:

1. **Raw Extractor (Layer 1)** -- Produces `RawPageSnapshot` from CDP data. Wraps existing `page.evaluate()` JS for DOM elements + browser-use's a11y tree. Pure extraction, no interpretation. Can parallelize CDP calls via `asyncio.gather`.

2. **Semantic Interpreter (Layer 2)** -- Transforms raw elements into `SemanticField` objects in Python (not JS). Sub-components: Type Classifier, Label Resolver, Field Grouper, Option Discoverer. This is the core value: moving opaque JS logic into testable, debuggable Python.

3. **State Tracker (Layer 3)** -- Maintains cross-step state. Diffs observations to detect reveals, value changes, page transitions. Uses MutationObserver dirty-set for targeted re-interpretation. Closes the observe-act-verify loop.

**Key architectural decisions:**
- **Strategy pattern** for extraction backends: DOM-only strategy first (closest to current), hybrid DOM+a11y strategy as target
- **Adapter pattern** for migration: `SemanticField` initially mirrors `FormField` interface; adapter bridges during cutover
- **LLM enters at exactly two points**: agent planner (browser-use) and fill answer generator (inside domhand_fill). Observation pipeline is LLM-free.
- **Observer builds on browser-use's `BrowserStateSummary`**, not parallel to it -- avoids duplicate CDP calls

### Critical Pitfalls

1. **Breaking the domhand_fill contract (C1)** -- `fill_executor.py` depends on `FormField`'s exact field names, `data-ff-id` tagging, and `field_type` string enum values. Prevention: define new model as strict superset of FormField, use adapter during migration, run both extractors in parallel shadow mode, never change field_type enum values.

2. **A11y tree gaps for custom widgets (C2)** -- Button groups have no ARIA role, Workday's `data-uxi-widget-type` is not a standard role, section headings lose DOM relationship in a11y tree, cross-shadow-root `aria-labelledby` breaks. Prevention: spike on 3 real platforms before committing; plan hybrid approach from day one; keep `_JS_DETECT_BUTTON_GROUPS` pass.

3. **Regression on working platforms (C3)** -- Six ATS platforms with unique widget patterns. Shipping "better for Workday but broken for Greenhouse" destroys user trust. Prevention: platform parity matrix with HTML fixtures, gate shipping on all-platform parity, platform-by-platform rollout behind feature flag.

4. **getFullAXTree performance on Workday (C4)** -- 5000+ DOM nodes can cause 2-8 second a11y tree fetch. Prevention: benchmark on real Workday pages in Phase 1 spike, use depth-limited fetch, set 2-second observation budget with fallback.

5. **LLM hallucination from oversized context (H5)** -- The number one documented problem. Prevention: section-scoped field context, viewport-scoped extraction, prune dropdown options to top 10 fuzzy matches, 2000-token budget per page observation.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: Contract Definition, Test Infrastructure, and Validation Spike

**Rationale:** Everything depends on two things being true: (a) the new observer can produce output compatible with the existing action layer, and (b) the a11y tree actually provides usable data for the target ATS platforms. Validate both before writing production code. Simultaneously, collect HTML fixtures -- they are the most valuable testing asset for the entire rebuild.

**Delivers:**
- Data model definitions (`Observation`, `RawPageSnapshot`, `SemanticField`, `ObservationState`)
- Adapter from `SemanticField` to `FormField` for backward compatibility
- HTML snapshot fixtures from 6 ATS platforms (2 pages per platform minimum)
- Golden-file expected outputs from current extractor on all fixtures
- CDP a11y tree spike results documenting gaps per platform
- Performance benchmarks for `getFullAXTree` on Workday/Oracle

**Addresses features:** Deterministic field discovery contract, stable field identity
**Avoids pitfalls:** C1 (contract breakage), C2 (a11y tree gaps discovered late), C4 (performance), M6 (no test infrastructure)

### Phase 2: Raw Extractor (Layer 1)

**Rationale:** Layer 1 is the foundation. It wraps existing extraction in the new interface, producing `RawPageSnapshot` from the same JS injection currently used. This is low-risk because it is a structural refactor of existing code, not new logic.

**Delivers:**
- `RawExtractor` class producing `RawPageSnapshot`
- Simplified `page.evaluate()` JS that extracts raw facts only (no type classification, no label resolution, no grouping)
- Parity tests: old `_EXTRACT_FIELDS_JS` output matches `RawExtractor` output on all fixtures

**Addresses features:** Field discovery, shadow DOM traversal, visibility detection, field tagging
**Uses stack:** CDP DOM.getDocument, DOMSnapshot.captureSnapshot, existing page.evaluate()

### Phase 3: Semantic Interpreter (Layer 2)

**Rationale:** This is the core value of the rebuild -- moving interpretation logic from opaque injected JS to testable Python. Depends on Layer 1 providing stable raw input.

**Delivers:**
- Type Classifier (canonical widget taxonomy)
- Label Resolver (with confidence scoring and sibling heuristics)
- Field Grouper (radio/checkbox groups, button groups, date component groups)
- Option Discoverer (native selects, ARIA-controlled listboxes, portal-based dropdowns)
- Section-scoped field partitioning for LLM context management
- `SemanticPageModel` output matching `ExtractionResult` shape via adapter

**Addresses features:** Canonical widget taxonomy, improved label resolution, semantic grouping, confidence scoring, section classification
**Avoids pitfalls:** H2 (grouping heuristics), H5 (LLM context size), M3 (Workday segmented dates)

### Phase 4: State Tracker (Layer 3) and domhand_fill Integration

**Rationale:** State tracking depends on stable `SemanticField` output. Integration with domhand_fill closes the loop and enables testing against real applications.

**Delivers:**
- `StateTracker` with field-level diff detection
- MutationObserver-based dirty-field tracking (attribute-filtered, debounced)
- DOM stability waiting (replacing hardcoded asyncio.sleep)
- Loading state detection
- Observation diff output for LLM context compression
- Feature-flagged integration: `GH_USE_NEW_OBSERVER=1` in domhand_fill
- Field completeness scoring per page

**Addresses features:** Observation diff, DOM stability waiting, loading state detection, conditional reveal detection, fill verification loop
**Avoids pitfalls:** H4 (MutationObserver flooding), H6 (stale state after navigation), H3 (dynamic IDs)

### Phase 5: Platform Parity and Rollout

**Rationale:** The new observer must match or beat the old extractor on ALL platforms before replacing it for ANY platform. This is a gating phase, not optional polish.

**Delivers:**
- Platform parity matrix: all 6 platforms passing golden-file tests
- Platform-by-platform feature flag rollout
- Platform detection heuristic (centralized)
- Widget fingerprint library (declarative pattern registry)
- iframe content extraction for Greenhouse/Oracle/Phenom

**Addresses features:** Cross-platform generality, platform detection, widget fingerprint library, fallback for unknown widgets
**Avoids pitfalls:** C3 (regression on working platforms), M1 (iframe isolation), M4 (Oracle opaque values)

### Phase 6: A11y Tree Enrichment (Strategy B)

**Rationale:** This is the highest-impact improvement but also the highest-risk change. By this point, the observer contract is stable and extraction strategies can be swapped cleanly via the Strategy pattern.

**Delivers:**
- `HybridExtractionStrategy` using browser-use's `EnhancedDOMTreeNode` (DOM + a11y tree merge)
- A11y-enriched label resolution (browser-computed accessible names vs hand-rolled heuristics)
- A11y-enriched state detection (checked, expanded, selected from a11y properties)
- Side-by-side comparison on all fixtures
- Gradual rollout per platform

**Uses stack:** CDP Accessibility.getFullAXTree, browser-use DomService integration
**Avoids pitfalls:** C2 (validated in Phase 1 spike), C4 (benchmarked, depth-limited)

### Phase Ordering Rationale

- **Phase 1 before everything:** You cannot build on an unvalidated foundation. The spike answers the two existential questions (contract compatibility, a11y tree viability) before any production code is written. Fixtures enable all subsequent testing.
- **Phases 2-3 before 4:** The pipeline has strict data dependencies (L2 needs L1, L3 needs L2). Building bottom-up is the only option.
- **Phase 4 before 5:** Integration with domhand_fill is required to test against real application flows. Platform parity testing needs the full pipeline wired up.
- **Phase 5 before 6:** A11y tree enrichment changes the extraction strategy. Platform parity must be established on the DOM-only strategy first, then re-validated on the hybrid strategy.
- **Phase 6 last:** Highest reward but highest risk. The Strategy pattern means it can be added without touching the interpreter or state tracker.

### Research Flags

**Phases likely needing deeper research during planning:**
- **Phase 1 (spike):** Must run `getFullAXTree` on real Workday, Oracle, Greenhouse pages and document every gap vs current extractor. This is a research-gated phase.
- **Phase 3 (interpreter):** Grouping heuristics for Oracle cx-select-pills and Workday button groups are complex. Need targeted research on sibling-based question-answer association patterns.
- **Phase 6 (a11y enrichment):** The hybrid DOM+a11y merge needs investigation into browser-use's `EnhancedDOMTreeNode` internals and how to efficiently consume it.

**Phases with standard patterns (skip research-phase):**
- **Phase 2 (raw extractor):** Wrapping existing JS in a new interface. Well-understood code.
- **Phase 4 (state tracker):** MutationObserver patterns are well-documented. DOM stability waiting is a standard debounce pattern.
- **Phase 5 (platform parity):** Testing and rollout process, no novel research needed.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | CDP APIs are authoritative documentation. browser-use source code is primary source. Gemini pricing verified. No new dependencies needed. |
| Features | HIGH | Grounded in existing codebase analysis and real ATS platform experience encoded in current code. Feature gaps are well-documented from production usage. |
| Architecture | HIGH | Three-layer pipeline validated against browser-use internals, academic patterns (WebArena, AgentOccam), and industry tools (rtrvr.ai, Stagehand). Existing codebase already implements pieces of each layer. |
| Pitfalls | HIGH | Pitfalls derived from actual production failures, codebase analysis, and platform-specific quirks already handled in current code. Not theoretical. |

**Overall confidence:** HIGH

### Gaps to Address

- **A11y tree coverage per platform:** The spike must produce a concrete gap matrix (which fields each platform exposes correctly in the a11y tree vs which need DOM fallback). This is the single biggest unknown.
- **getFullAXTree performance budget:** Need real benchmarks on Workday pages with 1000+ elements. If p95 exceeds 2 seconds, the hybrid strategy needs depth-limiting or lazy enrichment.
- **Cross-origin iframe handling:** Greenhouse and Oracle iframe patterns need investigation. browser-use has `max_iframes` config but Hand-X has not tested it systematically.
- **React Select / virtualized list observation:** Virtualized dropdown lists (only rendering visible options) are a known gap in option discovery. Need to validate whether a11y tree exposes all options or only rendered ones.
- **Workday "Select One" post-selection timing:** How long after a click does Workday update the DOM/a11y tree with the selected value? This affects settle-time configuration.

## Cost Analysis

| Scenario | DOM+A11y Extraction | Strategic Screenshots (Gemini Flash) | Total per Application |
|----------|--------------------|------------------------------------|----------------------|
| 5-page standard app | $0 (pure CDP) | $0.0004 (5 screenshots) | ~$0.0004 |
| 8-page Workday app | $0 (pure CDP) | $0.0006 (8 screenshots) | ~$0.0006 |
| With vision escalation (10% of fields) | $0 | $0.004 (50 field screenshots) | ~$0.004 |

Compare to current state: The fill LLM call cost dominates at ~$0.01-0.05 per application. Observation cost is negligible and should stay that way.

## Sources

### Primary (HIGH confidence)
- Chrome DevTools Protocol -- Accessibility, DOM, DOMSnapshot domains
- browser-use source code -- `dom/service.py`, `dom/views.py`, `agent/service.py`
- Hand-X source code -- `ghosthands/dom/field_extractor.py`, `fill_browser_scripts.py`, `verification_engine.py`, `fill_executor.py`, `shadow_helpers.py`
- Playwright ARIA Snapshot documentation
- MutationObserver MDN Web Docs
- Gemini/Claude/GPT-4o API pricing pages

### Secondary (MEDIUM confidence)
- WebArena (ICLR 2025) -- transition-focused observation abstraction
- AgentOccam (ICLR 2025) -- observation space refinement improving accuracy by 26.6 points
- SeeAct (ICML 2024) -- 20-25% accuracy gap between oracle and predicted grounding
- rtrvr.ai -- DOM intelligence achieving 81% accuracy vs 40-66% for vision agents
- Stagehand -- migration from raw DOM to Chrome Accessibility Tree
- Set-of-Mark Prompting (arXiv:2310.11441)

### Tertiary (LOW confidence)
- FocusAgent (arXiv:2510.03204) -- context trimming for web agents (preprint, not peer-reviewed)
- Shadow DOM/ARIA conflict research (Nolan Lawson, Alice Boxhall) -- documents real problems but solutions are still in spec development

---
*Research completed: 2026-04-02*
*Ready for roadmap: yes*
