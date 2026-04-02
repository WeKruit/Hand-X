# Technology Stack: Observation Layer v2.0

**Project:** Hand-X Observation Layer Rebuild
**Researched:** 2026-04-02
**Overall confidence:** HIGH (existing codebase + CDP/Playwright docs are authoritative)

---

## Recommended Stack

### Core Observation Pipeline (CDP + Playwright)

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| CDP `Accessibility.getFullAXTree` | Chrome 130+ | Semantic tree extraction | Already used by browser-use. Provides roles, names, descriptions, properties (checked, expanded, required, disabled) for every accessible node. Single call returns the full tree with parent/child relationships. |
| CDP `DOM.getDocument` | Chrome 130+ | Structural DOM tree | Provides HTML hierarchy, attributes, shadow roots, iframes. Needed for element targeting and action dispatch. |
| CDP `DOMSnapshot.captureSnapshot` | Chrome 130+ | Visual state (bounds, computed styles, paint order) | Single CDP call returns bounding boxes, computed styles, and visibility data for all nodes. No JS execution needed. |
| Playwright `page.accessibility.snapshot()` | Playwright 1.49+ | Quick a11y tree for validation/debugging | Returns YAML-like tree of roles/names. Useful for test assertions but too lossy for primary observation. |
| Playwright `locator.ariaSnapshot()` | Playwright 1.49+ | Per-element a11y snapshots for test assertions | YAML representation of accessibility subtree under a locator. Use for golden-file tests, NOT production observation. |

**Confidence: HIGH** -- browser-use already implements this exact CDP pipeline (see `browser_use/dom/service.py`). The existing `DomService._collect_all_trees()` fetches all three CDP trees in parallel and merges them into `EnhancedDOMTreeNode` structures.

### What the A11y Tree Gives You vs Raw DOM

| Signal | Raw DOM | A11y Tree | Winner |
|--------|---------|-----------|--------|
| Field semantics (role) | Must infer from tag+attributes+ARIA | Explicit `role` property | A11y tree |
| Accessible name (label) | Complex resolution chain (label[for], aria-labelledby, etc.) | Pre-computed `name` property | A11y tree |
| State (checked, expanded, selected) | Scattered across attributes, classes, ARIA attrs | Unified `properties` array | A11y tree |
| Required status | html `required`, `aria-required`, data attrs, label asterisks | `required` property | A11y tree (partial -- misses visual-only asterisks) |
| Options for select/listbox | Must query DOM for `[role=option]` children | `childIds` point to option nodes with names | A11y tree |
| Visual position | Not available | Not available (need DOMSnapshot) | Neither -- need snapshot |
| Shadow DOM content | Requires cross-shadow traversal | Flattened into single tree | A11y tree |
| Custom widget internals | Opaque without widget-specific logic | Only what ARIA exposes | Depends on ARIA quality |
| Dynamic class state | Available | Not exposed | Raw DOM |
| Element targeting for actions | Direct via selectors/backendNodeId | Only via backendDOMNodeId cross-reference | Raw DOM (but a11y provides backendDOMNodeId) |

**Key insight:** The a11y tree is the RIGHT primary signal for observation. Raw DOM is needed for action targeting. DOMSnapshot is needed for visual positioning. This is exactly what browser-use already does -- the rebuild should leverage this pipeline, not replace it.

### CDP Accessibility Tree API Details

```
Accessibility.getFullAXTree(depth?: integer, frameId?: string)
  Returns: { nodes: AXNode[] }

AXNode:
  nodeId: AXNodeId            -- unique within the tree
  ignored: boolean             -- whether assistive tech skips this
  ignoredReasons: AXProperty[] -- why ignored (if true)
  role: AXValue                -- "textbox", "combobox", "checkbox", etc.
  chromeRole: AXValue          -- Chrome's internal role name
  name: AXValue                -- computed accessible name
  description: AXValue         -- computed accessible description
  value: AXValue               -- current value
  properties: AXProperty[]     -- checked, expanded, disabled, required, etc.
  parentId: AXNodeId
  childIds: AXNodeId[]
  backendDOMNodeId: DOM.BackendNodeId  -- CRITICAL: links to DOM node
  frameId: Page.FrameId
```

**Limitations:**
- Cross-origin iframes require separate CDP connections per iframe
- `value` for custom widgets only reflects what ARIA authors expose (Workday is inconsistent)
- Ignored nodes still appear but with `ignored: true` -- must filter
- Enabling the accessibility domain slightly impacts page performance
- Some dynamic widgets (e.g., Workday's date pickers) update ARIA state lazily after visual change

### Playwright Accessibility APIs

**`page.accessibility.snapshot()`** returns:
```python
{
  "role": "WebArea",
  "name": "Page Title",
  "children": [
    {
      "role": "textbox",
      "name": "First Name",
      "value": "John",
      "required": true
    },
    {
      "role": "combobox",
      "name": "Country",
      "value": "United States",
      "expanded": false,
      "children": [...]  # only if expanded
    },
    {
      "role": "checkbox",
      "name": "I agree to terms",
      "checked": false
    }
  ]
}
```

**`locator.ariaSnapshot()`** returns YAML:
```yaml
- textbox "First Name": John
- combobox "Country" [expanded=false]: United States
- checkbox "I agree to terms"
- radiogroup "Gender":
  - radio "Male" [checked]
  - radio "Female"
```

**Maturity assessment:** MEDIUM. The YAML format is stable for snapshot testing but lacks:
- Bounding box information
- backendNodeId for action targeting
- Computed styles / visibility data
- Reliable state for custom ARIA widgets

**Recommendation:** Use `ariaSnapshot()` for test golden files. Use raw CDP `getFullAXTree` for production observation because you need backendNodeId and can fuse with DOM/snapshot data.

---

### State Tracking (MutationObserver)

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| `MutationObserver` | Web API (all browsers) | Real-time DOM change detection | Batch-async, fires after microtasks settle. Non-blocking via event loop. Can watch attributes, childList, characterData, subtree. |

**Configuration for form observation:**
```javascript
const observer = new MutationObserver((mutations) => {
  // Batched -- fires once per microtask checkpoint
  for (const mutation of mutations) {
    if (mutation.type === 'attributes') {
      // attributeName tells you WHICH attribute changed
      // mutation.target is the element
      // mutation.oldValue available if attributeOldValue: true
    }
  }
});

observer.observe(formContainer, {
  attributes: true,
  attributeFilter: [
    'aria-checked', 'aria-selected', 'aria-expanded',
    'aria-disabled', 'aria-required', 'aria-invalid',
    'value', 'checked', 'disabled', 'class',
    'data-state'  // Radix UI, shadcn patterns
  ],
  attributeOldValue: true,
  childList: true,       // new fields appearing
  subtree: true,         // entire form tree
  characterData: true,   // text content changes
});
```

**Performance characteristics:**
- Fires in batches (coalesced per microtask), NOT per-mutation
- Attribute monitoring with `attributeFilter` is efficient -- Chrome only tracks specified attrs
- `subtree: true` on a large form (100+ elements) is fine -- Chrome uses internal filtering
- Callback should be lightweight; queue mutations for async processing
- Does NOT detect `getComputedStyle` changes (only attribute mutations)
- Does NOT detect scroll position changes
- Does NOT fire for changes inside closed shadow DOM (must observe each shadow root separately)

**Use case in observation layer:** Install MutationObserver on form containers to detect:
1. New fields appearing (childList mutations) -- repeater sections, conditional fields
2. State changes (attribute mutations) -- checkbox checked, dropdown expanded, validation errors
3. Value changes (characterData) -- text being typed or auto-filled

**Do NOT use for:** Primary observation. MutationObserver tells you WHAT changed but not the semantic meaning. Use it as a change trigger that invalidates cached observation, then re-run the CDP pipeline for full state.

**Confidence: HIGH** -- Well-documented Web API, used extensively in browser-use's existing codebase.

---

### Visual Grouping (CSS Computed Style APIs)

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| CDP `DOMSnapshot.captureSnapshot` | Chrome 130+ | Bounding boxes + computed styles in one call | Already in pipeline. Returns bounds, paint order, computed styles for all laid-out nodes. |
| `getComputedStyle` (JS) | Web API | Fallback for specific style queries | Only needed when DOMSnapshot doesn't capture a specific property. |
| `getBoundingClientRect` (JS) | Web API | Element position relative to viewport | DOMSnapshot `bounds` gives document coordinates. Use this for viewport-relative positioning. |

**Visual proximity grouping strategy:**
```python
def group_by_visual_proximity(fields: list[ObservedField]) -> list[FieldGroup]:
    """
    Group fields that are visually adjacent and share a common heading/label.

    Algorithm:
    1. Sort fields by Y position (top to bottom)
    2. Detect section breaks: gap > 2x median inter-field gap
    3. Within sections, detect question groups: fields sharing a
       common preceding heading/label based on Y proximity
    4. Assign fields to their nearest preceding question text
    """
```

**Can we use visual proximity for semantic grouping?** YES, with caveats:
- Works well for standard form layouts (vertical stacking)
- Fails for multi-column layouts unless you also check X position
- Must combine with DOM hierarchy (fields in same `fieldset`, `form-group`, etc.)
- Best used as a secondary signal AFTER DOM structure analysis

**Confidence: MEDIUM** -- The approach works but needs DOM structure as primary signal.

---

### Strategic Vision (Per-Page Screenshots)

| Technology | Cost per 1080p screenshot | Purpose | When to Use |
|------------|--------------------------|---------|-------------|
| **Gemini 2.5 Flash** | ~$0.00008 (~258 tokens at $0.30/M) | Cheapest vision, adequate for layout understanding | Default choice for per-page observation |
| **Gemini 2.5 Pro** | ~$0.00032 (~258 tokens at $1.25/M) | Higher accuracy for complex layouts | Escalation when Flash confidence is low |
| **Claude Sonnet 4** | ~$0.005 (~1600 tokens at $3/M) | Most accurate for form understanding | NOT for routine observation (20x more expensive) |
| **GPT-4o** | ~$0.003 (~1000 tokens at $2.50/M) | Alternative to Claude for complex cases | Only if Claude is unavailable |

**Cost analysis for a typical 5-page job application:**
| Strategy | Gemini Flash | Gemini Pro | Claude Sonnet |
|----------|-------------|------------|---------------|
| Screenshot per page (5) | $0.0004 | $0.0016 | $0.025 |
| Screenshot per action (50 actions avg) | $0.004 | $0.016 | $0.25 |

**PROJECT.md says:** "Strategic screenshots OK, per-action screenshots not." This is correct. Per-page screenshots with Gemini Flash cost under $0.001 for a full application.

**Set-of-Mark (SoM) Prompting:**
SoM overlays numbered bounding boxes on screenshots, letting vision models reference specific DOM elements by number. This bridges the gap between "what the model sees" and "what element to act on."

Implementation:
1. Take screenshot
2. For each interactive element in the observation, draw a numbered bounding box overlay
3. Send annotated screenshot to vision model
4. Model responses reference element numbers, which map back to backendNodeIds

**Use for:** Page-level "what section am I looking at" / "are there visual elements the a11y tree missed" / repeater boundary detection.
**Do NOT use for:** Per-field state detection (CDP is deterministic and free).

**Confidence: HIGH** for costs, MEDIUM for SoM implementation details.

---

### Structured Output / Schema Enforcement

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| **Pydantic v2** | 2.x | Schema definition + validation | Already used throughout Hand-X. `FormField` and `ExtractionResult` are Pydantic models. |
| **LLM Structured Output (tool calling)** | Provider-specific | Force LLM to emit valid schemas | Gemini, Claude, and GPT all support tool-calling mode. Use Pydantic model as the schema definition. |
| **temperature=0** | N/A | Deterministic LLM output | Combined with structured output, eliminates randomness in observation interpretation. |

**Making observation deterministic:**

The observation layer itself should be 100% deterministic (same page = same output). LLM involvement should ONLY be for:
1. Semantic interpretation of observed data (what does this field MEAN)
2. Ambiguity resolution when structural signals conflict
3. Vision-based gap filling when a11y tree is incomplete

Schema enforcement pattern:
```python
from pydantic import BaseModel

class ObservedField(BaseModel):
    """Single field observation -- deterministic from CDP data."""
    backend_node_id: int
    role: str                    # from a11y tree
    name: str                    # computed accessible name
    value: str | None            # current value from a11y + DOM
    field_type: str              # canonical type: text, select, checkbox, radio-group, etc.
    required: bool               # from a11y properties
    checked: bool | None         # for checkboxes/radios
    expanded: bool | None        # for comboboxes/accordions
    disabled: bool               # from a11y properties
    options: list[str] | None    # for select/listbox
    bounding_box: DOMRect | None # from DOMSnapshot
    section: str                 # computed from heading hierarchy
    group_id: str | None         # links related fields (question + options)

class PageObservation(BaseModel):
    """Full page observation -- deterministic."""
    url: str
    title: str
    fields: list[ObservedField]
    sections: list[str]          # heading hierarchy
    has_submit_button: bool
    observation_hash: str        # SHA256 of sorted field data
    timestamp: float
```

**Confidence: HIGH** -- Pydantic is already the standard in this codebase.

---

## What Browser-Use Already Provides (DO NOT REBUILD)

The vendored `browser_use/dom/service.py` already implements a sophisticated observation pipeline:

1. **Parallel CDP collection:** `DOM.getDocument` + `Accessibility.getFullAXTree` + `DOMSnapshot.captureSnapshot` + viewport metrics + JS event listeners -- all in parallel
2. **Three-tree fusion:** Merges DOM structure, a11y semantics, and visual snapshot into `EnhancedDOMTreeNode`
3. **Smart serialization:** `DOMTreeSerializer` reduces ~10,000 DOM nodes to ~200 interactive elements
4. **Paint order filtering:** Hides elements behind overlays
5. **Shadow DOM traversal:** Full support via DOM tree walking
6. **Iframe handling:** Configurable depth and count limits
7. **Scroll detection:** Enhanced `is_actually_scrollable` combining CDP + CSS analysis

**What browser-use LACKS for Hand-X's needs:**
- Form-specific semantic grouping (question + its options as a unit)
- Field state interpretation (a11y `checked` property mapped to "this Yes/No question is answered Yes")
- Repeater section detection and boundary isolation
- Deterministic hash for "same page same observation"
- Section-level observation (only look at current form section, not entire page)

**The rebuild should be a LAYER ON TOP of browser-use's DomService, not a replacement.**

---

## Academic/Industry Tool Observation Strategies

### WebArena
- **Representation:** Accessibility tree as primary observation (same approach we recommend)
- **Format:** Flattened text representation of a11y nodes with [id] labels
- **Key finding (ICLR 2025):** "Transition-focused observation abstraction" -- rather than sending the full tree each step, describe only what CHANGED between observations. Reduces token consumption and improves accuracy.

### Mind2Web
- **Representation:** Full HTML with candidate element selection
- **Format:** Raw HTML is too large for LLM context, so they use a candidate ranking model to pre-filter to ~50 elements
- **Key finding:** HTML and accessibility tree are complementary -- neither alone is sufficient for all websites.

### browser-use (our vendored library)
- **Representation:** Enhanced DOM tree (DOM + a11y + snapshot fusion)
- **Observation:** Full page observation per step, serialized to indexed text
- **Limitation:** Generic agent observation, not optimized for form-filling. Includes links, headings, images in addition to form fields.

### Stagehand
- **Observation:** Moved from raw DOM to Chrome Accessibility Tree as primary signal
- **Key insight:** "Accessibility tree offers a cleaner, more reliable view by filtering out unnecessary noise"
- **Approach:** `observe()` uses LLM to find elements matching natural language descriptions. NOT deterministic.

### SeeAct
- **Observation:** Multimodal -- screenshot + HTML
- **Grounding strategy:** Action Generation (vision) then Action Grounding (map to HTML elements)
- **Key finding:** 20-25% accuracy gap between oracle grounding and predicted grounding. Grounding is the hard problem.

### LaVague
- **Observation:** Screenshot + DOM combined
- **Key trend (2025):** "Rich, redundant observations" combining text DOM, a11y tree, and screenshots is the industry consensus.

### rtrvr.ai (commercial, not open source)
- **Observation:** DOM-only, no screenshots
- **Key claim:** DOM intelligence achieves 81% accuracy vs 40-66% for vision-based agents
- **Performance:** 0.9 min per task vs 6-12 min for vision agents, $0.12 vs $0.50-$3.00 per task

**Consensus across all tools:**
1. Accessibility tree is the preferred primary observation signal
2. DOM structure supplements for targeting and structural grouping
3. Vision is useful for disambiguation but expensive and non-deterministic
4. Transition-focused observation (what changed) beats full-page dumps

---

## RPA Observation Techniques

### UiPath Selector Strategy
- **Multi-signal selectors:** XML fragments capturing element + ancestor attributes
- **Fuzzy matching:** Allows partial attribute matching when strict selectors break
- **Dynamic selectors:** Variables in selector templates for data-driven elements
- **Key practice:** Use `aaname` (accessible name) as primary identifier -- aligns with a11y tree approach

### Automation Anywhere
- **Approach:** XPath-based element identification
- **Fallback:** AI Computer Vision when DOM selectors fail

### UiPath AI Computer Vision
- **When DOM fails:** Computer Vision identifies UI elements from screenshots
- **Key insight:** Vision is the FALLBACK, not the primary approach. DOM-based identification is faster and more reliable.

**RPA consensus aligns with web agent consensus:** DOM/a11y first, vision as escalation.

---

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| Primary observation | CDP a11y tree + DOM + snapshot fusion | Raw DOM JS extraction (current `field_extractor.py`) | Current approach is non-deterministic, overwhelms LLM, misses semantics |
| Primary observation | CDP a11y tree + DOM + snapshot fusion | Screenshot + vision model | Non-deterministic, 10-50x more expensive, slower, less accurate for form data |
| Primary observation | CDP a11y tree + DOM + snapshot fusion | Playwright `page.accessibility.snapshot()` | Missing backendNodeId, bounding boxes, computed styles -- can't target elements for actions |
| State tracking | CDP re-query after MutationObserver trigger | Continuous polling | Wasteful; MutationObserver is event-driven and batched |
| State tracking | CDP re-query after MutationObserver trigger | MutationObserver only | Tells you WHAT changed but not semantic meaning; need full re-observation |
| Vision model | Gemini 2.5 Flash | Claude Sonnet | 20-60x more expensive per image; overkill for page-level screenshots |
| Structured output | Pydantic + tool calling | Free-form LLM text | Non-deterministic; can't validate; breaks downstream consumers |
| Semantic grouping | A11y tree hierarchy + visual proximity | DOM class-name heuristics | Fragile across ATS platforms; class names vary wildly |

---

## Installation

Already vendored in the codebase. No new dependencies required for core observation.

```bash
# Already in pyproject.toml:
# playwright (browser automation)
# pydantic (structured output)
# structlog (logging)

# For vision escalation (already available):
# google-generativeai or litellm (Gemini Flash access)
```

---

## Sources

- [Chrome DevTools Protocol - Accessibility Domain](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/) -- HIGH confidence
- [Playwright ARIA Snapshot Docs](https://playwright.dev/docs/aria-snapshots) -- HIGH confidence
- [browser-use Architecture (DeepWiki)](https://deepwiki.com/browser-use/browser-use) -- HIGH confidence (matches vendored source code)
- [rtrvr.ai DOM Intelligence Architecture](https://www.rtrvr.ai/blog/dom-intelligence-architecture) -- MEDIUM confidence (commercial claims)
- [WebArena (ICLR 2025) World Models paper](https://proceedings.iclr.cc/paper_files/paper/2025/file/a00548031e4647b13042c97c922fadf1-Paper-Conference.pdf) -- HIGH confidence
- [SeeAct (ICML'24)](https://osu-nlp-group.github.io/SeeAct/) -- HIGH confidence
- [Set-of-Mark Prompting (arXiv:2310.11441)](https://arxiv.org/abs/2310.11441) -- HIGH confidence
- [Gemini API Pricing](https://ai.google.dev/gemini-api/docs/pricing) -- HIGH confidence
- [Claude API Pricing](https://platform.claude.com/docs/en/about-claude/pricing) -- HIGH confidence
- [UiPath Selectors](https://docs.uipath.com/activities/other/latest/ui-automation/about-selectors) -- HIGH confidence
- [MutationObserver MDN](https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver) -- HIGH confidence
- Existing codebase: `browser_use/dom/service.py`, `ghosthands/dom/field_extractor.py` -- HIGH confidence (primary source)
