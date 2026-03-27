# Deep Research: Deterministic Form Field State Verification

**Date:** 2026-03-26
**Scope:** Shadow DOM, custom widgets, before/after snapshots, accessibility tree, CDP, MutationObserver
**Goal:** Catalog every deterministic (no LLM, no vision) technique for reading form field state across shadow DOM boundaries and custom widgets, with concrete code examples and trade-off analysis.

---

## Table of Contents

1. [Shadow DOM Observation (Open vs Closed)](#1-shadow-dom-observation)
2. [Custom Select/Dropdown Widget DOM Patterns](#2-custom-selectdropdown-widget-dom-patterns)
3. [Before/After Snapshot Comparison](#3-beforeafter-snapshot-comparison)
4. [Stagehand's observe() Approach](#4-stagehands-observe-approach)
5. [Playwright's Shadow DOM Handling](#5-playwrights-shadow-dom-handling)
6. [CDP DOMSnapshot.captureSnapshot](#6-cdp-domsnapshotcapturesnapshot)
7. [MutationObserver Approach](#7-mutationobserver-approach)
8. [Accessibility Tree as Verification Source](#8-accessibility-tree-as-verification-source)
9. [Synthesis: Recommended Verification Architecture](#9-synthesis)

---

## 1. Shadow DOM Observation

### 1.1 Open Shadow DOM

**JavaScript API:** `element.shadowRoot` returns the `ShadowRoot` when `mode: "open"`. Standard DOM APIs (`querySelector`, `querySelectorAll`, `getElementById`) work inside it.

**Playwright built-in:** Playwright's CSS and text locators pierce open shadow DOM by default. Every CSS descendant combinator pierces an arbitrary number of open shadow roots automatically. No special syntax needed.

**Reading values:** Once you have a reference to the input inside an open shadow root, `element.value`, `element.checked`, `element.selectedIndex` all work normally.

### 1.2 Closed Shadow DOM

**JavaScript API:** `element.shadowRoot` returns `null` for closed shadow roots. This is a hard boundary -- no standard JavaScript API can cross it.

**CDP workaround (the key finding):** Chrome DevTools Protocol can access closed shadow roots via `DOM.getDocument({ depth: -1, pierce: true })` or `DOM.describeNode({ nodeId, depth: -1, pierce: true })`. The `pierce` flag traverses both iframes AND shadow roots, including closed ones.

**Code example -- CDP piercing closed shadow DOM:**

```python
# Via Playwright CDPSession (Python)
client = await page.context.new_cdp_session(page)

# Option A: Get entire DOM tree with shadow roots flattened
doc = await client.send("DOM.getDocument", {"depth": -1, "pierce": True})
# doc["root"] contains the full tree including closed shadow root children

# Option B: Targeted query -- find element, then describe with pierce
doc = await client.send("DOM.getDocument", {})
body = await client.send("DOM.querySelector", {
    "nodeId": doc["root"]["nodeId"],
    "selector": "body"
})
described = await client.send("DOM.describeNode", {
    "nodeId": body["nodeId"],
    "depth": 0,
    "pierce": True
})
shadow_roots = described["node"].get("shadowRoots", [])
# Now query inside the shadow root
if shadow_roots:
    inner = await client.send("DOM.querySelector", {
        "nodeId": shadow_roots[0]["nodeId"],
        "selector": "input"
    })
```

**Another workaround -- monkey-patch `attachShadow`:** Override `Element.prototype.attachShadow` before page load to force `mode: "open"` for all shadow roots. This is the approach used by Patchright and some automation frameworks. Fragile, but works when you control the browser launch.

```javascript
// Inject before any page scripts run
const _attachShadow = Element.prototype.attachShadow;
Element.prototype.attachShadow = function(init) {
    return _attachShadow.call(this, { ...init, mode: "open" });
};
```

**Reading input values from closed shadow DOM via CDP:**

After obtaining a `nodeId` for an input inside a closed shadow root:
```python
# Resolve nodeId to a JS object
result = await client.send("DOM.resolveNode", {"nodeId": input_node_id})
object_id = result["object"]["objectId"]

# Read the .value property
value_result = await client.send("Runtime.callFunctionOn", {
    "objectId": object_id,
    "functionDeclaration": "function() { return this.value; }",
    "returnByValue": True
})
current_value = value_result["result"]["value"]
```

### 1.3 Hand-X Current Approach

Hand-X already has `shadow_helpers.py` which implements `window.__ff.allRoots()` -- a recursive shadow root collector that iterates through all open shadow roots. This works for open shadow DOM but cannot see closed roots. The `__ff.queryAll(selector)` function queries across all discovered roots.

**Gap:** No CDP-level closed shadow DOM access is currently implemented. For Oracle HCM (which uses open shadow DOM via OJET web components), the current approach is sufficient.

---

## 2. Custom Select/Dropdown Widget DOM Patterns

### 2.1 React-Select (JedWatson/react-select v5)

**DOM structure:**
```html
<div class="[prefix]__control">
  <div class="[prefix]__value-container">
    <div class="[prefix]__single-value">Selected Text Here</div>
    <!-- or for placeholder: -->
    <div class="[prefix]__placeholder">Select...</div>
    <input type="hidden" name="fieldName" value="selected_option_value" />
    <div class="[prefix]__input-container">
      <input id="react-select-..." type="text" aria-autocomplete="list" />
    </div>
  </div>
  <div class="[prefix]__indicators">...</div>
</div>
```

**How to read the selected value deterministically:**

| Method | Selector / API | What you get |
|--------|---------------|--------------|
| Hidden input | `input[type="hidden"][name="fieldName"]` | The option's `value` (often an ID) |
| Display text | `.[prefix]__single-value` | The visible label text |
| aria-selected | When menu is open: `[role="option"][aria-selected="true"]` | Selected option element |
| aria-live region | `.[prefix]__live-region` (with `aria-live="polite"`) | Screen reader announcement text |

**Best approach:** Query the hidden `<input>` for the form value, or the `__single-value` div's `textContent` for the display text.

### 2.2 Material UI Select (MUI v5)

**DOM structure:**
```html
<div class="MuiSelect-root">
  <div role="combobox" aria-expanded="false" aria-haspopup="listbox" tabindex="0">
    Selected Text
  </div>
  <input aria-hidden="true" tabindex="-1" name="fieldName" value="selected_value" />
  <svg><!-- dropdown arrow --></svg>
</div>
<!-- When open, a portal renders: -->
<ul role="listbox">
  <li role="option" aria-selected="true">Option 1</li>
  <li role="option" aria-selected="false">Option 2</li>
</ul>
```

**How to read:**

| Method | Selector / API | What you get |
|--------|---------------|--------------|
| Hidden input | `input[aria-hidden="true"][name="fieldName"]` | The raw value |
| Display div | `div[role="combobox"]` or `div[role="button"]` | `textContent` = displayed text |
| Open listbox | `li[role="option"][aria-selected="true"]` | Selected option (only when open) |

**Note:** MUI Select uses `role="button"` instead of the ARIA-recommended `role="combobox"`. The hidden input carries the form value.

### 2.3 Ant Design Select (antd v5)

**DOM structure:**
```html
<div class="ant-select ant-select-single">
  <div class="ant-select-selector">
    <span class="ant-select-selection-item" title="Selected Text">Selected Text</span>
    <span class="ant-select-selection-search">
      <input type="search" role="combobox" aria-expanded="false" />
    </span>
  </div>
</div>
<!-- Dropdown portal (rendered in document.body): -->
<div class="ant-select-dropdown">
  <div class="ant-select-item ant-select-item-option ant-select-item-option-selected">
    <div class="ant-select-item-option-content">Selected Text</div>
  </div>
</div>
```

**How to read:**

| Method | Selector / API | What you get |
|--------|---------------|--------------|
| Selection item | `.ant-select-selection-item` | `textContent` or `title` attribute = displayed value |
| Selected class | `.ant-select-item-option-selected` (in portal) | The selected option (only when open) |
| ARIA combobox | `input[role="combobox"]` | `.value` = search text, not selection |

**Key issue:** Ant Design does NOT render a hidden `<select>` or `<input type="hidden">`. The selected value lives only in `.ant-select-selection-item`. The dropdown portal is in `document.body`, disconnected from the component. Use `.ant-select-selector` class selectors, not ARIA roles (Ant's ARIA compliance is inconsistent).

### 2.4 Oracle JET (OJET) -- `oj-combobox-one`, `oj-select-one`

**DOM structure (open shadow DOM via custom elements):**
```html
<oj-combobox-one id="myField" value="selectedValue">
  #shadow-root (open)
    <div class="oj-combobox oj-component">
      <div class="oj-combobox-choice">
        <span class="oj-combobox-chosen">Displayed Text</span>
        <input type="text" class="oj-combobox-input" />
      </div>
    </div>
</oj-combobox-one>
```

**How to read:**

| Method | API | What you get |
|--------|-----|--------------|
| Element property | `element.value` on the custom element host | The raw value (programmatic) |
| Display text | Shadow root `.oj-combobox-chosen` | `textContent` = displayed label |
| Attribute | `element.getAttribute("value")` | May be stale -- property is more reliable |

**Critical caveat for automation:** Setting the value programmatically via `element.value = x` sets it visually but may NOT trigger Knockout.js observables, so the application may not recognize the change. The `[property]Changed` CustomEvent is how OJET communicates value changes. For verification (reading only), `element.value` is reliable.

**Hand-X current approach:** The `__ff.queryAll()` mechanism in `shadow_helpers.py` already traverses into OJET shadow roots. The `fill_executor.py` reads values via `page.evaluate()` scripts that access these shadow DOM elements.

### 2.5 Universal Patterns Across Custom Selects

| Signal | Reliability | Shadow DOM Safe | Notes |
|--------|-------------|-----------------|-------|
| `input[type="hidden"]` value | High (React-Select, MUI) | N/A (light DOM) | Not present in Ant Design or OJET |
| `.selected-value` display text | High | Requires traversal | Class names vary by library |
| `[aria-selected="true"]` | Medium | Requires traversal | Only reliable when dropdown is open |
| Custom element `.value` property | High (OJET) | Element host is light DOM | OJET, Salesforce Lightning, etc. |
| Accessibility tree `value` | High | Penetrates shadow DOM | See section 8 |

---

## 3. Before/After Snapshot Comparison

### 3.1 Concept

Take a structured snapshot of all form field values before performing fill actions, then take another snapshot after, and diff them. This approach answers "what changed?" rather than "does field X have value Y?"

### 3.2 Implementation with CDP DOMSnapshot

```python
async def capture_form_state(page) -> dict[str, str]:
    """Capture all input values on the page via CDP DOMSnapshot."""
    client = await page.context.new_cdp_session(page)
    result = await client.send("DOMSnapshot.captureSnapshot", {
        "computedStyles": []  # No computed styles needed for values
    })

    state = {}
    for doc in result["documents"]:
        strings = result["strings"]
        nodes = doc["nodes"]

        # Extract node names and types
        node_names = nodes.get("nodeName", [])
        node_values = nodes.get("nodeValue", [])
        input_values = nodes.get("inputValue", {})
        text_values = nodes.get("textValue", {})
        input_checked = nodes.get("inputChecked", {})
        option_selected = nodes.get("optionSelected", {})
        backend_ids = nodes.get("backendNodeId", [])
        attributes = nodes.get("attributes", [])

        # inputValue is RareStringData: {index: [...], value: [...]}
        if input_values:
            for idx, val_str_idx in zip(
                input_values.get("index", []),
                input_values.get("value", [])
            ):
                node_name = strings[node_names[idx]] if idx < len(node_names) else ""
                backend_id = backend_ids[idx] if idx < len(backend_ids) else idx
                value = strings[val_str_idx] if val_str_idx < len(strings) else ""
                state[f"input:{backend_id}"] = value

        # textValue for textareas
        if text_values:
            for idx, val_str_idx in zip(
                text_values.get("index", []),
                text_values.get("value", [])
            ):
                backend_id = backend_ids[idx] if idx < len(backend_ids) else idx
                value = strings[val_str_idx] if val_str_idx < len(strings) else ""
                state[f"textarea:{backend_id}"] = value

        # inputChecked for checkboxes/radios
        if input_checked:
            for idx in input_checked.get("index", []):
                backend_id = backend_ids[idx] if idx < len(backend_ids) else idx
                state[f"checked:{backend_id}"] = "true"

        # optionSelected for <option> elements
        if option_selected:
            for idx in option_selected.get("index", []):
                backend_id = backend_ids[idx] if idx < len(backend_ids) else idx
                state[f"selected:{backend_id}"] = "true"

    return state


async def diff_form_state(before: dict, after: dict) -> dict:
    """Return fields that changed between two snapshots."""
    changes = {}
    all_keys = set(before.keys()) | set(after.keys())
    for key in all_keys:
        old = before.get(key, "")
        new = after.get(key, "")
        if old != new:
            changes[key] = {"before": old, "after": new}
    return changes
```

### 3.3 Implementation with Playwright Accessibility Snapshot

```python
async def capture_ax_form_state(page) -> dict[str, dict]:
    """Capture form state via accessibility tree."""
    snapshot = await page.accessibility.snapshot(interesting_only=False)
    state = {}

    def walk(node, path=""):
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")
        checked = node.get("checked")
        selected = node.get("selected")

        if role in ("textbox", "combobox", "spinbutton", "searchbox",
                     "slider", "checkbox", "radio", "switch"):
            key = f"{role}:{name}" if name else f"{role}:{path}"
            state[key] = {
                "value": value,
                "checked": checked,
                "selected": selected,
                "role": role,
                "name": name,
            }

        for i, child in enumerate(node.get("children", [])):
            walk(child, f"{path}/{role}[{i}]")

    if snapshot:
        walk(snapshot)
    return state
```

### 3.4 Implementation with Hand-X's Existing Extractor

The most natural approach for Hand-X is using `extract_visible_form_fields(page)` which already returns structured `FormField` objects with `current_value`:

```python
# Pseudo-code using existing Hand-X infrastructure
before_fields = await extract_visible_form_fields(page)
before_state = {get_stable_field_key(f): f.current_value for f in before_fields}

# ... perform fill actions ...

after_fields = await extract_visible_form_fields(page)
after_state = {get_stable_field_key(f): f.current_value for f in after_fields}

# Diff
for key in set(before_state) | set(after_state):
    if before_state.get(key) != after_state.get(key):
        print(f"Changed: {key}: {before_state.get(key)!r} -> {after_state.get(key)!r}")
```

### 3.5 Trade-offs: Snapshot vs Individual Field Verification

| Approach | Pros | Cons |
|----------|------|------|
| **Before/after snapshot diff** | Catches unexpected side effects (field A fill changes field B); single point of comparison; no per-field read logic | Two full page scans (latency); new fields appearing between scans confuse the diff; does not explain WHY a field changed |
| **Individual field verification** (current Hand-X) | Precise per-field error messages; can retry specific fields; cheaper for single-field fills | Misses side effects on other fields; requires per-widget-type read logic |
| **Hybrid** | Best of both -- verify target field immediately, then periodic full-page audit | More complex to implement; two code paths |

**Recommendation:** The hybrid approach is strongest. Hand-X already does individual field verification (`fill_verify.py`). Adding a periodic full-page snapshot comparison (e.g., after each `domhand_fill` batch) would catch side effects without slowing down individual fills.

---

## 4. Stagehand's observe() Approach

### 4.1 Architecture

Stagehand evolved from raw DOM parsing to using the **Chrome Accessibility Tree** as its primary element enumeration source. Key architectural decisions:

1. **Accessibility tree first:** Rather than parsing raw DOM HTML, Stagehand reads Chrome's accessibility tree (via Playwright's `page.accessibility.snapshot()` or CDP `Accessibility.getFullAXTree`). This provides a semantic view filtered of decorative elements, reducing data size by 80-90% compared to raw DOM.

2. **DOM processing pipeline:**
   - Depth-first traversal of all frames (main + iframes)
   - For each frame: capture accessibility tree + absolute XPath mapping
   - Stitch frames into a single combined tree
   - Each element gets a unique `EncodedId` (frame ordinal + node ID)

3. **Shadow DOM handling:** Automatic -- the accessibility tree inherently penetrates open shadow DOM boundaries. Shadow DOM elements appear in the accessibility tree with their semantic roles/names/values regardless of encapsulation.

4. **Iframe handling:** Recursive frame traversal via Playwright's frame API, computing absolute XPaths for each iframe boundary crossing.

### 4.2 observe() Method

```typescript
// Stagehand observe() returns actionable elements
const elements = await stagehand.observe("interactive form fields");
// Returns: Array<{ selector: string, description: string, ... }>
```

`observe()` identifies interactive elements by:
1. Taking an accessibility tree snapshot of the page
2. Constructing a hierarchical string representation
3. Sending it to an LLM to identify elements matching the instruction
4. Returning element descriptors with XPath selectors

**Important:** Stagehand's observe() IS LLM-dependent for element selection. The accessibility tree enumeration is deterministic, but the "which elements match this instruction" step uses an LLM. For purely deterministic verification, use only the accessibility tree capture step, not the LLM selection.

### 4.3 Key Insight for Hand-X

Stagehand's move from DOM parsing to accessibility tree is significant. The accessibility tree:
- Penetrates open shadow DOM automatically
- Provides semantic `value` properties for form fields
- Filters decorative elements naturally
- Is stable across visual layout changes

Hand-X's current approach (`extract_visible_form_fields` via `__ff` JavaScript helpers) is DOM-parsing-based. Adding accessibility tree reads as a secondary verification channel would improve confidence, especially for custom widgets where DOM class names are unreliable.

---

## 5. Playwright's Shadow DOM Handling

### 5.1 Built-in CSS Piercing

Playwright's CSS engine pierces open shadow DOM by default:

```python
# This automatically searches inside open shadow roots
await page.locator("input.my-field").fill("value")

# Text locators also pierce shadow DOM
await page.get_by_text("Submit").click()

# Role locators pierce shadow DOM
await page.get_by_role("textbox", name="Email").fill("test@test.com")
```

**How it works internally:** The CSS engine's descendant combinator (space) and child combinator (>) both pierce open shadow roots. The engine searches light DOM first in iteration order, then recursively enters open shadow roots.

### 5.2 Limitations

| Limitation | Detail |
|-----------|--------|
| **Closed shadow DOM** | CSS, text, and role locators cannot penetrate closed shadow roots. `element.shadowRoot` returns `null`. |
| **XPath** | Does NOT pierce any shadow DOM (open or closed). XPath operates only on the light DOM tree. |
| **`page.evaluate()` with `querySelectorAll`** | Standard `document.querySelectorAll()` does NOT pierce shadow DOM. You must manually traverse via `element.shadowRoot.querySelectorAll()`. |
| **Slots** | Elements distributed into slots may not be found by locators in some edge cases (known Playwright issue #33547). |

### 5.3 `page.evaluate()` Shadow DOM Traversal

Standard `querySelectorAll` does NOT automatically pierce shadow DOM inside `page.evaluate()`. You must write recursive traversal:

```python
# This does NOT pierce shadow DOM:
result = await page.evaluate("""
    () => document.querySelectorAll('input').length
""")

# This DOES pierce shadow DOM (recursive approach):
result = await page.evaluate("""() => {
    function deepQueryAll(selector, root = document) {
        let results = [...root.querySelectorAll(selector)];
        // Check root's own shadow root
        if (root.shadowRoot) {
            results.push(...deepQueryAll(selector, root.shadowRoot));
        }
        // Check all descendant shadow roots
        root.querySelectorAll('*').forEach(el => {
            if (el.shadowRoot) {
                results.push(...deepQueryAll(selector, el.shadowRoot));
            }
        });
        return results;
    }
    return deepQueryAll('input').map(el => ({
        id: el.id,
        name: el.name,
        value: el.value,
        type: el.type
    }));
}""")
```

This is essentially what Hand-X's `__ff.queryAll()` does in `shadow_helpers.py`.

### 5.4 Third-Party Shadow DOM Piercing

The `query-selector-shadow-dom` npm package provides a `querySelectorAllDeep` function and can be registered as a Playwright selector engine. However, since Playwright v0.14.0+, the built-in CSS piercing makes this unnecessary for open shadow DOM.

### 5.5 Practical Guidance for Hand-X

For reading form field values:
- **Playwright locators** (CSS/text/role): Use for simple reads of open shadow DOM elements
- **`page.evaluate()` with `__ff.queryAll()`**: Use when you need batch enumeration across shadow roots (already implemented)
- **CDP `DOM.getDocument(pierce: true)`**: Use only when closed shadow DOM is encountered (not currently needed for Oracle HCM)

---

## 6. CDP DOMSnapshot.captureSnapshot

### 6.1 What It Returns

`DOMSnapshot.captureSnapshot` returns a comprehensive snapshot of the entire page DOM in a flattened format:

```python
result = await client.send("DOMSnapshot.captureSnapshot", {
    "computedStyles": ["display", "visibility"]  # optional
})

# result structure:
# {
#   "documents": [
#     {
#       "documentURL": <string_index>,
#       "nodes": {
#         "nodeName": [<string_index>, ...],
#         "nodeValue": [<string_index>, ...],
#         "backendNodeId": [<int>, ...],
#         "attributes": [[<key_idx, val_idx, ...>], ...],
#         "inputValue": { "index": [...], "value": [...] },  # RareStringData
#         "textValue": { "index": [...], "value": [...] },   # RareStringData
#         "inputChecked": { "index": [...] },                 # RareBooleanData
#         "optionSelected": { "index": [...] },               # RareBooleanData
#         "parentIndex": [...],
#         "shadowRootType": { "index": [...], "value": [...] }
#       },
#       "layout": { ... }
#     }
#   ],
#   "strings": ["", "INPUT", "div", ...]  # Shared string table
# }
```

### 6.2 Shadow DOM Handling

**Shadow DOM is flattened** in the returned tree. Shadow root boundaries are removed and the content appears inline in the document. The `shadowRootType` rare data tells you which nodes were originally shadow roots, but the tree is already flattened for traversal.

### 6.3 Form Field Value Extraction

| Property | Element Type | What It Contains |
|----------|-------------|-----------------|
| `inputValue` | `<input>` elements | The input's `.value` property (current user-entered text) |
| `textValue` | `<textarea>` elements | The textarea's `.value` property |
| `inputChecked` | `<input type="checkbox/radio">` | Boolean -- is this checked? |
| `optionSelected` | `<option>` elements | Boolean -- is this option selected? |

### 6.4 Limitations for Custom Widgets

**This is the critical gap:** `DOMSnapshot.captureSnapshot` only captures values for native HTML form elements (`<input>`, `<textarea>`, `<select>`/`<option>`). For custom widgets that don't use native form elements:

| Widget | Has native input? | DOMSnapshot captures value? |
|--------|-------------------|---------------------------|
| React-Select | Hidden `<input>` | Yes (the hidden input's value) |
| MUI Select | Hidden `<input>` | Yes |
| Ant Design Select | NO native input | **No** -- value is only in `<span>` text |
| Oracle OJET combobox | `<input>` inside shadow root | **Partial** -- shadow DOM flattened but input is for search text, not selected value |
| Custom `<div>` buttons | No | **No** |

### 6.5 browser-use's Usage

browser-use already uses `DOMSnapshot.captureSnapshot` extensively (see `browser_use/dom/enhanced_snapshot.py`). It captures computed styles, layout bounds, and node data. But it uses the snapshot primarily for **visibility and layout detection**, not for form value extraction. The `inputValue`, `textValue`, `inputChecked`, and `optionSelected` fields are available in the raw data but not currently surfaced to the form fill verification pipeline.

**Opportunity:** Wire `DOMSnapshot` form value data into the verification pipeline as a supplementary signal. This is essentially free since the snapshot is already being captured.

---

## 7. MutationObserver Approach

### 7.1 Concept

Install a `MutationObserver` before fill actions that records all DOM mutations. After actions complete, analyze the recorded mutations to determine what changed.

### 7.2 Implementation

```python
# Before fill: install observer
await page.evaluate("""() => {
    window.__formMutations = [];
    window.__formObserver = new MutationObserver((mutations) => {
        for (const m of mutations) {
            window.__formMutations.push({
                type: m.type,
                target: m.target.tagName + (m.target.id ? '#' + m.target.id : ''),
                attributeName: m.attributeName,
                oldValue: m.oldValue,
                newValue: m.type === 'attributes'
                    ? m.target.getAttribute(m.attributeName)
                    : null,
                addedNodes: m.addedNodes.length,
                removedNodes: m.removedNodes.length,
            });
        }
    });
    window.__formObserver.observe(document.body, {
        attributes: true,
        attributeOldValue: true,
        characterData: true,
        characterDataOldValue: true,
        childList: true,
        subtree: true,
    });
}""")

# ... perform fill actions ...

# After fill: collect and analyze mutations
mutations = await page.evaluate("""() => {
    window.__formObserver.disconnect();
    const result = window.__formMutations;
    window.__formMutations = [];
    return result;
}""")
```

### 7.3 Shadow DOM Limitation

**MutationObserver does NOT cross shadow DOM boundaries.** If you observe `document.body`, you will NOT see mutations inside any shadow root. This is a fundamental Web API constraint.

**Workaround:** Monkey-patch `attachShadow` to install observers on every shadow root:

```python
await page.evaluate("""() => {
    const originalAttachShadow = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function(init) {
        const shadowRoot = originalAttachShadow.call(this, init);
        // Attach observer to new shadow root
        window.__formObserver.observe(shadowRoot, {
            attributes: true,
            attributeOldValue: true,
            characterData: true,
            characterDataOldValue: true,
            childList: true,
            subtree: true,
        });
        return shadowRoot;
    };
}""")
```

**Problem:** This only catches shadow roots created AFTER the monkey-patch. For shadow roots that already exist, you must iterate and attach observers manually (which is what `ally.js` `observe/shadow-mutations` does).

### 7.4 Trade-offs vs Before/After Snapshot

| Factor | MutationObserver | Before/After Snapshot |
|--------|-----------------|----------------------|
| **What it captures** | Every intermediate mutation (attribute changes, node additions/removals) | Only the final state vs initial state |
| **Volume** | Can be enormous -- a single React-Select interaction may generate hundreds of mutations | Compact -- just two state dictionaries |
| **Shadow DOM** | Does NOT cross boundaries without monkey-patching | CDP DOMSnapshot flattens shadow DOM automatically |
| **Performance** | Active overhead during mutations; can slow page interactions | Two point-in-time captures; no runtime overhead |
| **Timing** | Captures async mutations that happen after your action returns | Requires waiting for DOM to settle before second snapshot |
| **Value extraction** | Records attribute changes, but NOT `.value` property changes on inputs (`.value` is a property, not an attribute) | DOMSnapshot captures `.value` properties directly |

### 7.5 Critical Limitation: `.value` Property Changes

**MutationObserver cannot detect `input.value` property changes.** When you type into an input or set `input.value = "foo"` programmatically, the DOM attribute does not change -- only the JavaScript property does. MutationObserver only watches DOM attributes and tree changes, not JavaScript property mutations.

This means MutationObserver is **fundamentally unsuitable** as the primary verification mechanism for form field values. It can detect:
- Attribute changes (class, aria-selected, aria-checked, data-* attributes)
- Child node additions/removals (dropdown menus opening/closing)
- Text content changes in display elements

But it CANNOT detect the most common form fill result: an input's `.value` changing.

**Verdict:** MutationObserver is useful as a supplementary signal (detecting dropdown menu state, validation error appearances, class changes) but cannot replace snapshot-based value reads.

---

## 8. Accessibility Tree as Verification Source

### 8.1 CDP `Accessibility.getFullAXTree`

```python
client = await page.context.new_cdp_session(page)
result = await client.send("Accessibility.getFullAXTree", {})
# result["nodes"] is a flat array of AXNode objects
```

Each `AXNode` contains:
```json
{
    "nodeId": "12",
    "role": {"type": "role", "value": "textbox"},
    "name": {"type": "computedString", "value": "Email Address"},
    "value": {"type": "string", "value": "user@example.com"},
    "properties": [
        {"name": "focused", "value": {"type": "boolean", "value": false}},
        {"name": "required", "value": {"type": "boolean", "value": true}},
        {"name": "invalid", "value": {"type": "token", "value": "false"}}
    ],
    "backendDOMNodeId": 45,
    "childIds": [...]
}
```

### 8.2 Playwright `page.accessibility.snapshot()`

**Note: This API is deprecated** but still functional and useful.

```python
snapshot = await page.accessibility.snapshot(interesting_only=False)
# Returns a tree of dicts, each with:
# {
#   "role": "textbox",
#   "name": "Email Address",
#   "value": "user@example.com",
#   "focused": False,
#   "required": True,
#   ...
#   "children": [...]
# }
```

**Properties available per node:**
- `role` -- ARIA role (textbox, combobox, checkbox, radio, listbox, option, etc.)
- `name` -- Computed accessible name
- `value` -- Current value (for textbox: entered text; for combobox: selected option text)
- `checked` -- For checkbox/radio/switch: "mixed", True, or False
- `selected` -- For option elements: True/False
- `disabled`, `expanded`, `focused`, `required`, `readonly`

### 8.3 Shadow DOM Penetration

**YES -- the accessibility tree penetrates open shadow DOM automatically.** The Chrome accessibility tree is computed from the rendered composited view of the page, not from the light DOM tree. Shadow DOM encapsulation is a DOM-level concept; the accessibility tree is computed post-composition and sees all elements regardless of shadow boundaries.

**For closed shadow DOM:** The accessibility tree ALSO penetrates closed shadow roots. The browser's accessibility engine has privileged access to all DOM content regardless of shadow mode.

**This is the single most important finding:** The accessibility tree is the ONLY mechanism that deterministically penetrates both open and closed shadow DOM while providing semantic form field values, without requiring any JavaScript injection or CDP DOM traversal.

### 8.4 Custom Widget Coverage

| Widget | AX Tree Role | AX Tree Value | Reliable? |
|--------|-------------|---------------|-----------|
| `<input type="text">` | `textbox` | Current `.value` | Yes |
| `<textarea>` | `textbox` (multiline) | Current `.value` | Yes |
| `<select>` | `combobox` or `listbox` | Selected option text | Yes |
| `<input type="checkbox">` | `checkbox` | N/A (use `checked`) | Yes |
| `<input type="radio">` | `radio` | N/A (use `checked`) | Yes |
| React-Select | `combobox` | Selected option display text | Yes |
| MUI Select | `combobox` or `button` | Displayed text | Mostly (role varies) |
| Ant Design Select | `combobox` | Selected option text | Yes |
| Oracle OJET combobox | `combobox` | Selected option text | Yes |
| Custom `div[role=combobox]` | `combobox` | Depends on ARIA attrs | If ARIA implemented |
| File input | `button` or `textbox` | File name | Browser-dependent |

### 8.5 Playwright ARIA Snapshot Testing (Modern Replacement)

Playwright's newer ARIA snapshot testing (non-deprecated) provides YAML-based accessibility tree comparison:

```python
# Capture and assert accessibility structure
await expect(page.locator("form")).to_match_aria_snapshot("""
- textbox "Email": user@example.com
- textbox "Password"
- checkbox "Remember me" [checked]
- button "Sign In"
""")
```

The YAML format captures:
- Textbox values: `- textbox "Label": current value text`
- Checkbox state: `- checkbox "Label" [checked]`
- Selected state: `- option "Option Text" [selected]`
- Expanded state: `- combobox "Label" [expanded]`

### 8.6 Code Example: Form State via Accessibility Tree

```python
async def capture_form_state_via_ax(page) -> dict[str, dict]:
    """Read all form field values via accessibility tree.

    Penetrates shadow DOM. Works with custom widgets.
    Returns {name_or_role_path: {role, name, value, checked, ...}}.
    """
    snapshot = await page.accessibility.snapshot(interesting_only=False)
    if not snapshot:
        return {}

    fields = {}
    form_roles = {
        "textbox", "combobox", "listbox", "searchbox",
        "spinbutton", "slider", "checkbox", "radio", "switch",
    }

    def walk(node, path_parts=None):
        if path_parts is None:
            path_parts = []

        role = node.get("role", "")
        name = node.get("name", "")

        if role in form_roles:
            key = name if name else "/".join(path_parts + [role])
            fields[key] = {
                "role": role,
                "name": name,
                "value": node.get("value", ""),
                "checked": node.get("checked"),
                "selected": node.get("selected"),
                "disabled": node.get("disabled"),
                "required": node.get("required"),
                "invalid": node.get("invalid"),
                "focused": node.get("focused"),
            }

        children = node.get("children", [])
        for i, child in enumerate(children):
            child_role = child.get("role", "none")
            walk(child, path_parts + [f"{child_role}[{i}]"])

    walk(snapshot)
    return fields


async def verify_field_via_ax(page, field_name: str, expected_value: str) -> bool:
    """Verify a specific field's value via the accessibility tree."""
    state = await capture_form_state_via_ax(page)
    field = state.get(field_name)
    if not field:
        return False
    current = field.get("value", "")
    return _normalize_for_comparison(current) == _normalize_for_comparison(expected_value)


def _normalize_for_comparison(s: str) -> str:
    """Normalize whitespace and casing for comparison."""
    return " ".join(s.strip().split()).lower()
```

### 8.7 CDP `Accessibility.getFullAXTree` for Deeper Control

When Playwright's deprecated `snapshot()` is insufficient:

```python
async def get_full_ax_tree(page) -> list[dict]:
    """Get the full accessibility tree via CDP."""
    client = await page.context.new_cdp_session(page)
    # Enable accessibility domain for consistent node IDs
    await client.send("Accessibility.enable")
    result = await client.send("Accessibility.getFullAXTree", {})
    return result["nodes"]


async def find_ax_node_by_name(page, name: str, role: str = None) -> dict | None:
    """Find an AX node by accessible name and optional role."""
    nodes = await get_full_ax_tree(page)
    for node in nodes:
        node_name = node.get("name", {}).get("value", "")
        node_role = node.get("role", {}).get("value", "")
        if node_name == name:
            if role is None or node_role == role:
                return node
    return None


async def read_field_value_via_ax(page, field_name: str) -> str | None:
    """Read a form field's current value via CDP accessibility tree."""
    node = await find_ax_node_by_name(page, field_name)
    if not node:
        return None
    value_obj = node.get("value", {})
    return value_obj.get("value") if value_obj else None
```

### 8.8 Performance Consideration

`Accessibility.getFullAXTree` and `page.accessibility.snapshot()` are fast (typically 10-50ms) because the browser maintains the accessibility tree incrementally. However, on very complex pages (Oracle HCM with 1000+ form fields across multiple sections), the full tree can be large. For targeted verification, `Accessibility.queryAXTree` (CDP) can search for specific nodes without fetching the entire tree:

```python
# Query specific node by name
result = await client.send("Accessibility.queryAXTree", {
    "accessibleName": "Email Address",
    "role": "textbox",
})
# Returns matching nodes without full tree traversal
```

---

## 9. Synthesis: Recommended Verification Architecture

### 9.1 Tier Model

Based on this research, here is the recommended verification architecture ordered by reliability and cost:

```
Tier 1: DOM Property Read (current Hand-X approach)
  - page.evaluate() with __ff helpers
  - Reads .value, .checked, .selectedIndex directly
  - Fast (<5ms), deterministic, works for native elements + open shadow DOM
  - LIMITATION: Requires per-widget-type read logic; fails on closed shadow DOM

Tier 2: Accessibility Tree Read (NEW -- highest value add)
  - page.accessibility.snapshot() or CDP Accessibility.getFullAXTree
  - Returns semantic value/checked/selected for ALL form field types
  - Penetrates ALL shadow DOM (open AND closed)
  - Works with custom widgets IF they expose ARIA attributes
  - ~10-50ms, deterministic, widget-agnostic
  - LIMITATION: Deprecated Playwright API; value depends on widget ARIA compliance

Tier 3: CDP DOMSnapshot Value Extraction (NEW -- supplementary)
  - Already captured by browser-use; just needs value fields surfaced
  - inputValue, textValue, inputChecked, optionSelected
  - Shadow DOM flattened automatically
  - Only works for native HTML form elements, not custom widget display text
  - Essentially free (data already fetched)

Tier 4: Before/After Snapshot Diff (NEW -- batch verification)
  - Any of Tier 1-3 used to take two snapshots and diff
  - Catches side effects (filling field A changed field B)
  - Use after batch fills, not per-field
  - ~20-100ms per snapshot

Tier 5: LLM Screenshot Verification (current Hand-X fallback)
  - fill_llm_escalation.py llm_verify_field_value()
  - Non-deterministic but handles visual edge cases
  - Expensive ($), non-deterministic, last resort
```

### 9.2 Concrete Implementation Recommendation

**Add accessibility tree as a secondary verification channel in `fill_verify.py`:**

```python
async def _verify_fill_via_accessibility(
    page, field: FormField, expected_value: str
) -> bool | None:
    """Accessibility-tree-based fill verification.

    Returns True if verified, False if contradicted, None if inconclusive
    (e.g., field not found in AX tree).
    """
    try:
        snapshot = await page.accessibility.snapshot(interesting_only=False)
        if not snapshot:
            return None

        # Search for the field by name
        target_name = field.name or field.raw_label or ""
        match = _find_ax_node(snapshot, target_name, field.field_type)
        if not match:
            return None

        ax_value = match.get("value", "")
        if field.field_type in ("checkbox", "toggle"):
            ax_checked = match.get("checked")
            desired_checked = not _is_explicit_false(expected_value)
            return ax_checked == desired_checked
        elif field.field_type in ("radio-group", "radio", "button-group"):
            return _field_value_matches_expected(
                str(ax_value), expected_value
            )
        else:
            return _field_value_matches_expected(
                str(ax_value), expected_value
            )
    except Exception:
        return None
```

**Add DOMSnapshot value extraction to `enhanced_snapshot.py`:**

The `build_snapshot_lookup` function in `browser_use/dom/enhanced_snapshot.py` already processes the snapshot but does not extract `inputValue`/`textValue`. Adding these fields to `EnhancedSnapshotNode` would make them available throughout the pipeline.

### 9.3 Approach Comparison Matrix

| Technique | Shadow DOM (Open) | Shadow DOM (Closed) | Custom Widgets | Native Inputs | Performance | Deterministic |
|-----------|-------------------|---------------------|----------------|---------------|-------------|---------------|
| `page.evaluate` + `__ff` | Yes | No | Per-widget logic | Yes | <5ms | Yes |
| Playwright CSS locators | Yes | No | Limited | Yes | <10ms | Yes |
| AX tree snapshot | Yes | **Yes** | If ARIA compliant | Yes | 10-50ms | Yes |
| CDP `DOMSnapshot` | Yes (flattened) | Yes (flattened) | Native only | Yes | 20-50ms | Yes |
| CDP `DOM.getDocument(pierce)` | Yes | **Yes** | Manual traversal | Yes | 10-30ms | Yes |
| MutationObserver | No (without hack) | No | Attribute changes only | **No** (`.value` invisible) | Real-time | Yes |
| LLM screenshot | Yes | Yes | Yes | Yes | 500ms+ | **No** |

### 9.4 Key Takeaways

1. **The accessibility tree is the single best deterministic verification source** for custom widgets across shadow DOM. It is the only mechanism that penetrates closed shadow DOM, works with custom widgets (if ARIA-compliant), and returns semantic form field values -- all without JavaScript injection.

2. **MutationObserver is NOT suitable** for form value verification because it cannot detect `.value` property changes on input elements. It can only supplement other methods by detecting attribute/class changes.

3. **CDP DOMSnapshot is already being captured** by browser-use but its form value fields (`inputValue`, `textValue`, `inputChecked`, `optionSelected`) are not currently surfaced. This is low-hanging fruit.

4. **Before/after snapshot diffing** is most valuable as a batch post-fill audit to catch side effects, not as the primary per-field verification mechanism.

5. **Hand-X's current `__ff` helper approach** is solid for open shadow DOM and native inputs. The main gap is custom widgets where the display text and underlying value diverge (React-Select, OJET combobox).

6. **For Oracle HCM specifically:** OJET uses open shadow DOM, so `__ff.queryAll()` works. The accessibility tree would be the best supplementary channel since OJET components generally have good ARIA compliance.

---

## Sources

- [Piercing the Shadow Root Using CDP (Yotam's Blog)](https://yotam.net/posts/piercing-the-shadow-root-using-cdp/)
- [Playwright Shadow DOM Documentation](https://playwright.dev/docs/locators)
- [Playwright ARIA Snapshot Testing](https://playwright.dev/docs/aria-snapshots)
- [Playwright Accessibility API](https://playwright.dev/python/docs/api/class-accessibility)
- [Chrome DevTools Protocol -- DOMSnapshot Domain](https://chromedevtools.github.io/devtools-protocol/tot/DOMSnapshot/)
- [Chrome DevTools Protocol -- Accessibility Domain](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)
- [Chrome DevTools Protocol -- DOM Domain](https://chromedevtools.github.io/devtools-protocol/tot/DOM/)
- [Stagehand v3 (Browserbase)](https://www.browserbase.com/blog/stagehand-v3)
- [Stagehand Architecture Breakdown (memo.d.foundation)](https://memo.d.foundation/breakdown/stagehand)
- [Stagehand observe() Documentation](https://docs.stagehand.dev/v3/basics/observe)
- [browser-use Interactive Element Detection (DeepWiki)](https://deepwiki.com/browser-use/browser-use/5.3-interactive-element-detection)
- [query-selector-shadow-dom (npm)](https://www.npmjs.com/package/query-selector-shadow-dom)
- [Recursive querySelectorAll piercing shadow DOM (GitHub Gist)](https://gist.github.com/Haprog/848fc451c25da00b540e6d34c301e96a)
- [MutationObserver MDN Documentation](https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver)
- [MutationObserver Shadow DOM Limitation (whatwg/dom #1287)](https://github.com/whatwg/dom/issues/1287)
- [Oracle JET combobox Documentation](https://www.oracle.com/webfolder/technetwork/jet/jsdocs/oj.ojComboboxOne.html)
- [Playwright Feature Request: Closed Shadow DOM (Issue #23047)](https://github.com/microsoft/playwright/issues/23047)
- [React-Select API Documentation](https://react-select.com/props)
- [Material UI Select API](https://mui.com/material-ui/api/select/)
- [Ant Design Select Testing Issues (Issue #23009)](https://github.com/ant-design/ant-design/issues/23009)
- [Full Accessibility Tree in Chrome DevTools (Chrome Blog)](https://developer.chrome.com/blog/full-accessibility-tree)
- [Stagehand iframe Handling (Browserbase Blog)](https://www.browserbase.com/blog/taming-iframes-a-stagehand-update)
- [ally.js Shadow Mutations Observer](https://allyjs.io/api/observe/shadow-mutations.html)
- [Detect DOM Changes with Mutation Observers (Chrome Blog)](https://developer.chrome.com/blog/detect-dom-changes-with-mutation-observers)
