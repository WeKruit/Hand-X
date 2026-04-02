# Architecture: Observation Layer Rebuild

**Domain:** Browser automation observation layer for ATS form filling
**Researched:** 2026-04-02
**Overall confidence:** HIGH (grounded in existing codebase analysis + browser-use internals + academic/industry patterns)

---

## 1. Layered Observation Architecture

### The Three Layers

```
Layer 3: State Tracker      ── what changed since last observation
Layer 2: Semantic Interpreter ── what each element means
Layer 1: Raw Extractor       ── what's physically on the page
```

### Layer 1: Raw Extractor

**Responsibility:** Produce a deterministic snapshot of all interactive elements on the page with their attributes, positions, and DOM context. No interpretation -- just facts.

**Inputs:** Playwright `Page` object (or CDP session)

**Outputs:** `RawPageSnapshot` containing:
- List of `RawElement` objects (field_id, tag, role, attributes, bounding rect, visibility, DOM path)
- Page metadata (URL, title, headings, stepper label)
- DOM fingerprint hash for identity comparison

**Implementation:** Single `page.evaluate()` call that returns a JSON array. This is essentially what `_EXTRACT_FIELDS_JS` does today, but restructured to be a pure extraction layer without semantic interpretation mixed in.

**Key difference from current:** The current `_EXTRACT_FIELDS_JS` mixes raw extraction with semantic decisions (type classification, label resolution, option discovery, grouping). The new Layer 1 extracts raw facts only. Type classification like "role=combobox -> type=select" moves to Layer 2.

### Layer 2: Semantic Interpreter

**Responsibility:** Transform raw elements into semantic form field representations. This is where field type classification, label resolution, grouping, and option discovery happen.

**Inputs:** `RawPageSnapshot` from Layer 1, optional `ObservationContext` from Layer 3

**Outputs:** `SemanticPageModel` containing:
- List of `SemanticField` objects (the successor to `FormField`)
- Section groupings with hierarchical structure
- Repeater section boundaries
- Page-level classification (auth page, form page, review page, confirmation)

**Sub-components:**
1. **Type Classifier** -- maps raw role/tag/attributes to canonical field types (text, select, radio_group, checkbox_group, date, file, toggle, button_group)
2. **Label Resolver** -- resolves accessible name via ARIA chain, wrapper traversal, previous sibling heuristics (largely what `firstMeaningfulWrapperLabel` does in current JS)
3. **Grouper** -- associates radio buttons, checkboxes, and options with their parent questions; identifies repeater sections
4. **Option Discoverer** -- extracts available options for select-like fields (native options, ARIA-controlled listbox items, visible option containers)

**Key architectural decision:** Layer 2 runs in Python, not JavaScript. The current system does too much in injected JS (type classification, label resolution, grouping), making it hard to debug and impossible to unit test. Layer 1 extracts raw data via JS; Layer 2 interprets it in Python where we have proper logging, error handling, and testability.

### Layer 3: State Tracker

**Responsibility:** Maintain observation state across agent steps. Detect what changed, what was filled, what was revealed, and what needs attention.

**Inputs:** Current `SemanticPageModel`, previous `ObservationState`, action results from the action layer

**Outputs:** `ObservationState` containing:
- Field-level state map (field_key -> {value, filled_by, verified, changed_since_last})
- Newly revealed fields (conditional reveals after fills)
- Fields with validation errors
- Page transition detection (same page vs. new page vs. SPA navigation)
- Scroll state (what's visible, what's below fold)

### Composition: Synchronous Pipeline

Use a **synchronous pipeline**, not event-driven or parallel composition:

```python
class Observer:
    async def observe(self, page: Page, context: ObservationContext | None = None) -> Observation:
        raw = await self.extractor.extract(page)          # Layer 1
        semantic = self.interpreter.interpret(raw, context) # Layer 2
        state = self.tracker.update(semantic, context)      # Layer 3
        return Observation(raw=raw, semantic=semantic, state=state)
```

**Why pipeline, not event-driven:**
- The layers have strict data dependencies (L2 needs L1 output, L3 needs L2 output)
- The observation happens at discrete points in the agent loop (before decision, after action), not continuously
- Event-driven would add complexity without benefit since we already have the browser-use step loop as our clock
- Pipeline is easier to test, debug, and reason about

**Why not parallel:**
- L2 depends on L1 output, L3 depends on L2 output -- they cannot run in parallel
- Within Layer 1, we can parallelize CDP calls (DOM snapshot + a11y tree + screenshot fetch can run concurrently via `asyncio.gather`)

---

## 2. Observer Contract / Interface Design

### The Observation Data Model

```python
@dataclass
class Observation:
    """Complete observation result passed to the decision layer."""
    raw: RawPageSnapshot          # Layer 1 output
    semantic: SemanticPageModel   # Layer 2 output
    state: ObservationState       # Layer 3 output
    metadata: ObservationMetadata # Timing, costs, strategy used

@dataclass
class RawPageSnapshot:
    """Raw extraction from the page -- no interpretation."""
    elements: list[RawElement]
    page_url: str
    page_title: str
    headings: list[str]
    stepper_label: str
    dom_fingerprint: str          # MD5 of structural signature
    timestamp: float

@dataclass
class RawElement:
    """A single interactive element as extracted from the DOM."""
    element_id: str               # Stable ID (data-ff-id or generated)
    tag: str                      # HTML tag name
    role: str | None              # ARIA role
    attributes: dict[str, str]    # All relevant attributes
    bounding_rect: DOMRect | None # Position and dimensions
    is_visible: bool
    dom_path: str                 # Stable path for re-location
    accessible_name: str          # Computed accessible name
    accessible_description: str
    parent_group_id: str | None   # fieldset/radiogroup/etc. container ID
    options_raw: list[dict]       # Raw option data (for selects/radios)

@dataclass
class SemanticField:
    """A form field with semantic meaning -- successor to FormField."""
    field_id: str
    field_key: str                # Stable identity key (survives re-extraction)
    field_type: FieldType         # Enum: text, email, select, radio_group, etc.
    label: str                    # Resolved human-readable label
    section: str                  # Section this field belongs to
    required: bool
    options: list[str]            # Cleaned option list
    current_value: str            # Current visible value
    is_native: bool               # Native HTML vs. custom widget
    widget_kind: str | None       # Sub-classification for fill strategy
    placeholder: str
    format_hint: str | None
    group_fields: list[str]       # Component field IDs (for grouped date, etc.)
    repeater_info: RepeaterInfo | None

@dataclass
class SemanticPageModel:
    """Semantic understanding of the entire page."""
    fields: list[SemanticField]
    sections: list[Section]
    page_type: PageType           # Enum: auth, form, review, confirmation
    page_marker: str              # Human-readable page identifier
    repeater_sections: list[RepeaterSection]

@dataclass
class ObservationState:
    """Cross-step state tracking."""
    field_states: dict[str, FieldState]
    newly_revealed: list[str]     # field_keys revealed since last observation
    validation_errors: list[ValidationError]
    page_changed: bool            # True if page transitioned
    fields_changed: list[str]     # field_keys whose values changed
    observation_count: int        # How many times we've observed this page
```

### Observer Input Contract

```python
@dataclass
class ObservationContext:
    """Context passed to the observer from the agent loop."""
    previous_state: ObservationState | None
    last_action_results: list[FillFieldResult] | None
    profile_data: dict[str, Any]  # For context-aware interpretation
    platform_hint: str            # workday, greenhouse, etc.
    page_url: str
    step_number: int
```

### How Observation Feeds the Decision Layer

The decision layer (browser-use Agent LLM) receives observation data via two paths:

1. **Browser-use's native path:** `BrowserStateSummary` with DOM tree + screenshot (unchanged -- this is what the LLM sees for navigation decisions)
2. **DomHand's path:** `Observation` object consumed by `domhand_fill`, `domhand_assess_state`, etc. (the action tools that the LLM invokes)

The LLM does NOT see raw `SemanticField` lists directly. Instead:
- `domhand_fill` uses `Observation.semantic.fields` internally to generate answers and fill
- `domhand_assess_state` uses `Observation.state` to classify page state and report blockers
- The LLM sees the *results* of these tools, not the raw observation

This is the correct architecture: the LLM is a planner/decision-maker, not a field-level executor. Field-level decisions (what value goes in what field) are handled by the cheap fill-LLM call inside `domhand_fill`.

### How the Action Layer Reports Back

```python
@dataclass
class ActionFeedback:
    """What the action layer reports to the observer after acting."""
    field_results: list[FillFieldResult]
    fields_attempted: list[str]   # field_keys
    fields_succeeded: list[str]
    fields_failed: list[str]
    page_navigated: bool          # True if a navigation occurred
```

The observer's `State Tracker` (Layer 3) accepts `ActionFeedback` to update `ObservationState`. This closes the observe-act-verify loop.

### Design Patterns

**Strategy Pattern for extraction backends:**

```python
class ExtractionStrategy(Protocol):
    async def extract(self, page: Page) -> RawPageSnapshot: ...

class DOMExtractionStrategy:
    """Current approach: JS injection to traverse DOM."""
    async def extract(self, page: Page) -> RawPageSnapshot: ...

class A11yTreeExtractionStrategy:
    """CDP Accessibility.getFullAXTree as primary source."""
    async def extract(self, page: Page) -> RawPageSnapshot: ...

class HybridExtractionStrategy:
    """A11y tree for semantics + DOM for structure."""
    async def extract(self, page: Page) -> RawPageSnapshot: ...
```

This allows swapping extraction backends without changing the semantic interpreter or state tracker. Build the interface first, implement DOM strategy first (closest to current), then add a11y tree strategy.

**Observer Pattern for state changes:**

Not needed at the macro level (the pipeline model is sufficient), but useful within Layer 3 for notifying the agent loop about significant state changes:

```python
class StateChangeCallback(Protocol):
    def on_fields_revealed(self, fields: list[SemanticField]) -> None: ...
    def on_validation_errors(self, errors: list[ValidationError]) -> None: ...
    def on_page_transition(self, from_page: str, to_page: str) -> None: ...
```

---

## 3. Observation Strategy Analysis

### Strategy A: Pure Accessibility Tree

**How:** Use CDP `Accessibility.getFullAXTree()` as the sole data source. Parse AX nodes for role, name, value, properties. Ignore DOM entirely.

**Pros:**
- Browser-computed semantics (roles, names, states) rather than hand-rolled heuristics
- Handles custom widgets correctly if they have proper ARIA markup
- Already available in browser-use (see `DomService._get_ax_tree_for_all_frames()`)
- Significantly fewer elements to process (a11y tree filters non-interactive content)
- Deterministic -- same ARIA markup always produces same a11y tree

**Cons:**
- Missing information: a11y tree lacks DOM structure needed for fill actions (no CSS selectors, no data attributes, no `data-automation-id`)
- Cannot fill fields with only a11y node IDs -- need DOM node references for Playwright actions
- ATS custom widgets often have POOR ARIA markup (Workday is decent, but many others are not)
- No bounding rect information in standard a11y tree (need separate CDP call)
- Option lists for closed dropdowns are not in the a11y tree (they don't exist in DOM until opened)

**Verdict: NOT viable as sole source.** The a11y tree lacks the DOM anchoring needed for actions, and ATS ARIA quality is too inconsistent.

### Strategy B: Hybrid DOM + Accessibility Tree (RECOMMENDED)

**How:** DOM traversal for structure and element identification (Layer 1), a11y tree for semantic enrichment (Layer 2). Merge by backend node ID.

**Pros:**
- DOM provides stable element references for fill actions (selectors, data-ff-id, tag structure)
- A11y tree provides computed accessible names, roles, and states (better than hand-parsing ARIA attributes)
- Graceful degradation: if a11y tree is missing info for an element, fall back to DOM-only heuristics
- Can use a11y tree's `checked`, `selected`, `expanded` states for reliable state detection
- browser-use already does this merge (`EnhancedDOMTreeNode` + `EnhancedAXNode`)

**Cons:**
- Two CDP calls per observation (DOM snapshot + a11y tree), though these can run in parallel
- Merge logic needed to associate DOM nodes with AX nodes (by backend_node_id)
- Slightly more complex than pure DOM approach

**Cost:** Two CDP round-trips (~50-100ms total, parallelized). No LLM cost at extraction time.

**Verdict: RECOMMENDED.** This is the natural evolution. browser-use already builds an `EnhancedDOMTreeNode` that merges DOM + a11y data. We should leverage this rather than maintaining our own separate extraction.

### Strategy C: Hybrid A11y Tree + Vision

**How:** A11y tree as primary extraction, strategic screenshots for verification of ambiguous elements.

**Pros:**
- Screenshots can verify visual state that DOM/a11y can't capture (e.g., custom-rendered checkmarks)
- Visual verification catches "looks right to user" vs. "DOM says X"

**Cons:**
- Screenshot processing requires vision LLM call (~$0.01-0.03 per screenshot for GPT-4o/Gemini)
- Per-page screenshots add 500ms+ latency
- PROJECT.md explicitly says "strategic screenshots OK, per-action screenshots NOT" -- budget constraint
- Vision LLM interpretation is non-deterministic (same screenshot can get different interpretations)
- Already available via browser-use's `use_vision="auto"` for stuck-field recovery

**Cost:** $0.01-0.03 per vision call + 500ms latency per screenshot. Multiplied across 20-50 fields per page = unacceptable if per-field.

**Verdict: Valid as an escalation path, not as primary strategy.** Use for verification of fills that DOM/a11y state detection can't confirm. This is already what `use_vision="auto"` provides.

### Strategy D: Vision-First with DOM Anchoring

**How:** Screenshot as primary observation (visual understanding of what's on the page), DOM selectors only for action anchoring.

**Pros:**
- Works regardless of DOM/ARIA quality
- Closest to how a human perceives the page

**Cons:**
- Extremely expensive: every observation requires a vision LLM call ($0.01-0.03)
- Non-deterministic: vision interpretation varies across calls
- Cannot extract option lists from screenshots (dropdown must be opened)
- Slow: 500ms+ per vision call
- Fundamentally incompatible with the "single cheap LLM call for all fields" architecture of domhand_fill
- browser-use already provides this as a fallback -- no need to rebuild it

**Verdict: REJECTED.** Incompatible with cost and determinism requirements. browser-use's vision fallback already serves this role for edge cases.

### Strategy Comparison Matrix

| Criterion | A: Pure A11y | B: DOM+A11y (REC) | C: A11y+Vision | D: Vision-First |
|-----------|-------------|-------------------|----------------|-----------------|
| Determinism | HIGH | HIGH | LOW | LOW |
| Cost/observation | ~$0 | ~$0 | $0.01-0.03 | $0.01-0.03 |
| Latency | ~50ms | ~80ms | ~600ms | ~600ms |
| Custom widget support | MEDIUM | HIGH | HIGH | HIGH |
| Action anchoring | POOR | EXCELLENT | POOR | MEDIUM |
| State detection | GOOD | EXCELLENT | GOOD | MEDIUM |
| Testability | HIGH | HIGH | LOW | LOW |
| ATS compatibility | MEDIUM | HIGH | HIGH | HIGH |

---

## 4. State Management Architecture

### Approach: Hybrid (MutationObserver + Re-extract on Demand)

**Event-driven (MutationObserver)** for detecting changes between agent steps:
- Inject a lightweight MutationObserver that tracks which `data-ff-id` elements had attribute/child mutations
- Records a "dirty set" of element IDs that changed
- Does NOT do full re-extraction on every mutation (too noisy, especially during fills)

**Re-extract on demand** at observation boundaries:
- Full re-extraction happens at defined points: before each agent step, after `domhand_fill` rounds, when state assessment is requested
- Uses the MutationObserver's dirty set to optimize: only re-interpret changed elements in Layer 2

```python
class StateTracker:
    def __init__(self):
        self._field_states: dict[str, FieldState] = {}
        self._dirty_field_ids: set[str] = set()
        self._page_fingerprint: str = ""

    async def install_mutation_watcher(self, page: Page) -> None:
        """Inject MutationObserver that records dirty element IDs."""
        await page.evaluate(MUTATION_WATCHER_JS)

    async def get_dirty_fields(self, page: Page) -> set[str]:
        """Retrieve and clear the dirty set from the page."""
        dirty = await page.evaluate("() => window.__ffDirty?.flush() || []")
        return set(dirty)

    def update(self, semantic: SemanticPageModel, context: ObservationContext | None) -> ObservationState:
        """Update state based on new observation."""
        # Detect page transition
        new_fingerprint = self._compute_fingerprint(semantic)
        page_changed = new_fingerprint != self._page_fingerprint

        if page_changed:
            self._field_states.clear()
            self._page_fingerprint = new_fingerprint

        # Update field states
        newly_revealed = []
        fields_changed = []
        for field in semantic.fields:
            prev = self._field_states.get(field.field_key)
            if prev is None:
                newly_revealed.append(field.field_key)
            elif prev.value != field.current_value:
                fields_changed.append(field.field_key)

            self._field_states[field.field_key] = FieldState(
                value=field.current_value,
                filled=(field.current_value and not is_placeholder_value(field.current_value)),
                verified=False,  # Updated by action feedback
            )

        return ObservationState(
            field_states=dict(self._field_states),
            newly_revealed=newly_revealed,
            fields_changed=fields_changed,
            page_changed=page_changed,
            validation_errors=self._detect_validation_errors(semantic),
            observation_count=self._observation_count,
        )
```

### "Did My Action Work?" Verification Loop

This is currently handled inside `domhand_fill`'s multi-round extraction. The new architecture formalizes it:

```
1. domhand_fill calls observer.observe() -> gets Observation with field states
2. domhand_fill fills fields via fill_executor
3. domhand_fill calls observer.observe() again -> gets updated Observation
4. State Tracker computes diff: which fields changed? which didn't?
5. Fields that didn't change despite fill attempt -> mark as failed, escalate
6. New fields revealed? -> add to next fill round
7. Repeat up to MAX_FILL_ROUNDS
```

The key insight: **the observer doesn't need to know about fill rounds.** It just observes. The fill action queries it repeatedly. The state tracker accumulates state across calls.

### Conditional Field Reveals

Detected by the State Tracker comparing field sets across observations:

```python
def _detect_reveals(self, current_fields: set[str], previous_fields: set[str]) -> list[str]:
    """Fields present now that weren't present before."""
    return list(current_fields - previous_fields)
```

Combined with the MutationObserver's dirty set, we can distinguish:
- **New fields appearing** (conditional reveal after a selection)
- **Existing fields changing value** (dropdown selection committed)
- **Fields disappearing** (section collapsed, conditional hide)

---

## 5. Integration Points with Existing System

### How the New Observer Feeds domhand_fill

**Current contract (`domhand_fill` expects):**
- `list[FormField]` from `page.evaluate(_EXTRACT_FIELDS_JS)`
- Each `FormField` has: `field_id`, `name`, `field_type`, `section`, `required`, `options`, `current_value`, etc.

**New contract:**
- `Observation` from `observer.observe(page, context)`
- `Observation.semantic.fields` returns `list[SemanticField]`
- `SemanticField` is the successor to `FormField` with the same essential data

**Migration path:**
1. `SemanticField` initially mirrors `FormField`'s interface (same field names, same types)
2. `domhand_fill` calls `observer.observe()` instead of `page.evaluate(_EXTRACT_FIELDS_JS)`
3. Internal fill logic receives `list[SemanticField]` where it currently receives `list[FormField]`
4. Adaptor function `semantic_field_to_form_field()` bridges during migration
5. Once all consumers migrated, remove `FormField` and the adaptor

**Files that change:**
- `ghosthands/actions/domhand_fill.py` -- call observer instead of raw JS extraction
- `ghosthands/actions/domhand_assess_state.py` -- use `ObservationState` for page classification
- `ghosthands/dom/fill_browser_scripts.py` -- Layer 1 JS becomes simpler (raw extraction only)
- `ghosthands/actions/views.py` -- `FormField` eventually replaced by `SemanticField`

### Integration with browser-use Agent Loop

The observer integrates at two points in the agent step:

1. **Before LLM decision (`_prepare_context`):** browser-use already calls `get_browser_state_summary()` which builds the DOM+a11y tree. The observer can leverage this same data rather than doing a separate extraction. Specifically, `BrowserStateSummary.dom_state` already contains `EnhancedDOMTreeNode` with merged DOM+a11y data.

2. **During tool execution:** When the LLM calls `domhand_fill` or `domhand_assess_state`, these tools invoke `observer.observe()` which can reuse the cached browser state or do a fresh extraction.

**Key insight:** browser-use's `DomService` already does the expensive work of fetching DOM snapshot + a11y tree + computing visibility. The new observer should build ON TOP of this data, not parallel to it.

```python
class Observer:
    async def observe_from_browser_state(
        self,
        browser_state: BrowserStateSummary,
        page: Page,
        context: ObservationContext | None = None,
    ) -> Observation:
        """Build observation from browser-use's already-fetched state."""
        raw = self.extractor.extract_from_dom_state(browser_state.dom_state)
        semantic = self.interpreter.interpret(raw, context)
        state = self.tracker.update(semantic, context)
        return Observation(raw=raw, semantic=semantic, state=state)
```

### Integration with Platform Guardrails

Platform detection (`detect_platform(url)`) feeds into `ObservationContext.platform_hint`. The semantic interpreter uses this to:
- Apply platform-specific grouping heuristics (Workday's `data-automation-id` patterns vs. Greenhouse's class-based patterns)
- Recognize platform-specific widget types (Workday's segmented date inputs, Oracle's type-ahead comboboxes)
- Adjust label resolution strategy per platform

Platform guardrails in the system prompt remain unchanged -- they guide the LLM's tool selection, not the observer's extraction.

### Backward Compatibility During Migration

**Phase 1 (parallel operation):**
- New observer exists alongside current `_EXTRACT_FIELDS_JS`
- `domhand_fill` has a feature flag: `USE_NEW_OBSERVER=true/false`
- Both paths produce `list[FormField]` (new observer via adaptor)
- Compare outputs in test suite -- must match on known fixtures

**Phase 2 (cutover):**
- New observer becomes default
- Old `_EXTRACT_FIELDS_JS` kept as fallback (env var toggle)
- `FormField` -> `SemanticField` migration

**Phase 3 (cleanup):**
- Remove old extraction code
- Remove `FormField` adaptor
- Remove fallback toggle

---

## 6. Context Management for LLM

### Two LLMs, Two Context Strategies

**Agent LLM (Gemini Flash -- planner):**
- Sees: browser-use's DOM tree representation + screenshot (via `BrowserStateSummary`)
- Does NOT see raw field lists
- Sees: tool results from `domhand_fill` ("filled 15/17 fields, 2 failed: Country dropdown, Salary field") and `domhand_assess_state` ("3 unresolved required fields")
- Context managed by browser-use's `MessageManager` (history compaction, message limiting)

**Fill LLM (Gemini Flash / Haiku -- answer generator):**
- Sees: field list + profile data in a single call
- This is where field-level context matters

### Compression Strategies for Fill LLM

**1. Section-scoped extraction:**
When `target_section` is specified, only send fields from that section. This is already implemented via `_filter_fields_for_scope()`.

**2. Progressive field list:**
For re-fill rounds after initial fill:
- Only include unfilled/failed fields (not already-filled ones)
- Include context from filled fields as "already answered" summary
- This reduces token count from ~2000 (all fields) to ~200 (remaining blockers)

**3. Field summarization:**
For the agent LLM's context, summarize the observation rather than showing raw data:
```
Page: "Work Experience" (step 3 of 5)
Sections: Personal Info [COMPLETE], Work Experience [3 UNFILLED]
Unfilled required: Country (dropdown), Start Date (date), Job Title (text)
```

**4. History pruning:**
AgentOccam's insight applies: once a page is filled and advanced, its observation history can be pruned from context. Only the current page's observation matters. The agent LLM already benefits from browser-use's `max_history_items` setting.

### Academic Context Management Patterns

From [AgentOccam](https://arxiv.org/html/2410.13825v1) (ICLR 2025):
- Simplify observations by removing redundant elements and converting to concise Markdown
- Not all previous steps' observations need retention; prune based on planning tree
- Observation space refinement alone improved WebArena scores by 26.6 points

From [FocusAgent](https://arxiv.org/html/2510.03204):
- Context trimming for web agents: identify which parts of the page are relevant to current task
- Filter DOM elements by proximity to current focus area

**Application to Hand-X:** The current `domhand_fill` already does context management well by using a single cheap LLM call with field list + profile. The main improvement is reducing noise in the field list itself (better extraction = fewer junk fields = less LLM confusion).

---

## 7. Data Flow Diagram

### Complete Pipeline

```
Page Load / SPA Navigation
         |
         v
[1. Raw Extraction]  -----> RawPageSnapshot
  - page.evaluate() for DOM elements    |
  - CDP a11y tree (via browser-use)     |    (asyncio.gather for
  - Page metadata scan                  |     parallel CDP calls)
         |
         v
[2. Semantic Interpretation]  -----> SemanticPageModel
  - Type classification (Python)        |
  - Label resolution (Python)           |    (NO LLM here --
  - Field grouping (Python)             |     pure heuristics)
  - Option discovery (Python)           |
  - Repeater detection (Python)         |
         |
         v
[3. State Tracking]  -----> ObservationState
  - Diff against previous state         |
  - Detect reveals/changes              |    (NO LLM here)
  - Incorporate action feedback         |
  - Page transition detection           |
         |
         v
[Observation Result]  -----> Observation
         |
    +----+----+
    |         |
    v         v
[domhand_fill]    [domhand_assess_state]
    |                    |
    v                    v
[Fill LLM Call]    [State Classification]
  (Haiku/Flash)      (pure Python)
    |                    |
    v                    v
[Fill Executor]    [ApplicationState]
    |              (returned to agent LLM)
    v
[Action Feedback] -----> back to State Tracker
    |
    v
[Re-observe if needed]
  (up to MAX_FILL_ROUNDS)
```

### Where the LLM Enters

The LLM enters at exactly TWO points:

1. **Agent LLM (planner):** After browser-use's `_prepare_context()`, decides which tool to call. Sees DOM tree + screenshot, NOT raw field lists.

2. **Fill LLM (answer generator):** Inside `domhand_fill`, after observation produces `SemanticPageModel.fields`. Receives field list + profile, returns answer map. Single call per fill round.

The observation pipeline itself (Layers 1-3) is **LLM-free**. This is intentional: observation must be deterministic and cheap. The same page should always produce the same `SemanticPageModel`.

### Where Screenshots Fit

Screenshots are **not part of the observation pipeline**. They serve two separate purposes:

1. **Agent LLM context:** browser-use always captures a screenshot in `_prepare_context()` for the planner LLM. This is outside the observer.

2. **Verification escalation:** When `domhand_fill` can't verify a fill via DOM state (Layer 3), it can request a vision-based verification as a fallback. This is an escalation path, not the primary observation.

---

## 8. Suggested Build Order

### Phase 1: Observer Interface + Raw Extractor (Build First)

**What:** Define the `Observation`, `RawPageSnapshot`, `SemanticField`, `ObservationState` data models. Implement `RawExtractor` that wraps the existing `_EXTRACT_FIELDS_JS` to produce `RawPageSnapshot`.

**Why first:** Establishes the contract. All other work depends on these interfaces. The raw extractor wraps existing code, so it's low-risk.

**Files to create:**
- `ghosthands/observer/__init__.py`
- `ghosthands/observer/models.py` (all data models)
- `ghosthands/observer/extractor.py` (Layer 1)
- `ghosthands/observer/observer.py` (pipeline coordinator)

**Integration:** None yet -- observer exists but isn't wired in.

### Phase 2: Semantic Interpreter (Port + Improve)

**What:** Port type classification, label resolution, grouping, and option discovery from JS to Python. Initially, produce output identical to current `FormField`.

**Why second:** This is the core value -- moving interpretation logic from opaque JS to testable Python.

**Files to create:**
- `ghosthands/observer/interpreter.py` (Layer 2 coordinator)
- `ghosthands/observer/type_classifier.py`
- `ghosthands/observer/label_resolver.py`
- `ghosthands/observer/field_grouper.py`
- `ghosthands/observer/option_discoverer.py`

**Integration:** Write parity tests comparing old `_EXTRACT_FIELDS_JS` output with new interpreter output on fixture pages.

### Phase 3: State Tracker + MutationObserver

**What:** Implement cross-step state tracking with dirty-field detection via MutationObserver.

**Why third:** Depends on having stable `SemanticField` output from Phase 2.

**Files to create:**
- `ghosthands/observer/state_tracker.py` (Layer 3)
- `ghosthands/observer/mutation_watcher.js` (injected watcher)

**Integration:** Wire into `domhand_fill`'s multi-round loop. Replace manual re-extraction with observer re-observation.

### Phase 4: domhand_fill Integration

**What:** Wire observer into `domhand_fill`. Feature-flagged: `GH_USE_NEW_OBSERVER=1`.

**Files to change:**
- `ghosthands/actions/domhand_fill.py` -- call `observer.observe()` instead of `page.evaluate(_EXTRACT_FIELDS_JS)`
- `ghosthands/actions/domhand_assess_state.py` -- use `ObservationState`

**Integration:** Run against all ATS fixtures. A/B comparison with old path.

### Phase 5: A11y Tree Enrichment (Strategy B)

**What:** Add `A11yEnrichedExtractionStrategy` that uses browser-use's `EnhancedDOMTreeNode` (which already merges DOM + a11y tree) instead of raw JS extraction.

**Why last:** This is the most impactful improvement but has the highest risk. By this point, the observer contract is stable and we can swap extraction strategies cleanly.

**Files to create:**
- `ghosthands/observer/strategies/dom_strategy.py` (current, default)
- `ghosthands/observer/strategies/hybrid_strategy.py` (DOM + a11y tree)

**Integration:** Side-by-side comparison on all ATS fixtures. Gradual rollout.

---

## 9. Component Boundary Diagram

```
+-------------------------------------------+
|            browser-use Agent               |
|  (agent loop, LLM planner, step hooks)    |
+-------------------------------------------+
         |                    ^
         | calls tools        | tool results
         v                    |
+-------------------------------------------+
|          DomHand Actions Layer             |
|  domhand_fill  |  domhand_assess_state    |
|  domhand_select | domhand_interact_control|
+-------------------------------------------+
         |                    ^
         | observe()          | ActionFeedback
         v                    |
+-------------------------------------------+
|              Observer                       |
|  +----------+  +-----------+  +----------+ |
|  | Extractor|->|Interpreter|->|  State   | |
|  | (Layer 1)|  | (Layer 2) |  | Tracker  | |
|  +----------+  +-----------+  | (Layer 3)| |
|       |                       +----------+ |
|       v                                    |
|  [Extraction Strategy]                     |
|  - DOMStrategy (current)                   |
|  - HybridStrategy (DOM+a11y, target)       |
+-------------------------------------------+
         |
         v
+-------------------------------------------+
|          Fill Executor                     |
|  (per-control-type fill strategies)        |
|  (unchanged from current architecture)     |
+-------------------------------------------+
         |
         v
+-------------------------------------------+
|          Playwright / CDP                  |
|  (page.evaluate, page.click, CDP calls)   |
+-------------------------------------------+
```

**Boundary rules:**
- Observer NEVER fills fields or clicks elements
- Fill Executor NEVER extracts or interprets fields
- DomHand Actions are the only bridge between observation and action
- browser-use Agent only sees tool results, never raw observation data
- State Tracker is the only component that maintains cross-step memory

---

## Sources

- [rtrvr.ai DOM Intelligence Architecture](https://www.rtrvr.ai/blog/dom-intelligence-architecture) -- DOM-native observation approach, why screenshots reduce performance
- [AgentOccam: A Simple Yet Strong Baseline for LLM-Based Web Agents](https://arxiv.org/html/2410.13825v1) -- Observation space refinement, context pruning strategies
- [Chrome DevTools Protocol - Accessibility domain](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/) -- CDP getFullAXTree API reference
- [WebArena: A Realistic Web Environment](https://webarena.dev/static/paper.pdf) -- Web agent benchmarks and observation representations
- [FocusAgent: Trimming Large Context of Web Agents](https://arxiv.org/html/2510.03204) -- Context management by focus area
- [MutationObserver - MDN Web Docs](https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver) -- Event-driven DOM change detection
- [Full accessibility tree in Chrome DevTools](https://developer.chrome.com/blog/full-accessibility-tree) -- Chrome a11y tree implementation details
- [Building Browser Agents: Architecture, Security, and Practical Solutions](https://arxiv.org/html/2511.19477v1) -- Browser agent architecture patterns
- browser-use source code: `browser_use/dom/service.py`, `browser_use/dom/views.py`, `browser_use/agent/service.py` -- Existing DOM+a11y tree merge implementation
- Hand-X source code: `ghosthands/actions/domhand_fill.py`, `ghosthands/dom/fill_browser_scripts.py`, `ghosthands/actions/views.py` -- Current observation and action implementation
