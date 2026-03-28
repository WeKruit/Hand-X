# TODO: Workday ATS Test Fixture

## Why

DomHand has `platforms/workday.py` with `data-automation-id` selectors but NO test fixture validates them end-to-end. Workday is NOT shadow DOM — it's React + CSS-in-JS with hashed class names (`css-*`). All form controls are in the light DOM.

## Workday-Specific Control Patterns (from Intel Careers page)

### 1. Text input
- `<input type="text" id="workExperience-135--jobTitle" name="jobTitle" aria-required="true" class="css-el9z0t">`
- Label: `<label for="workExperience-135--jobTitle">Job Title<abbr>*</abbr></label>`
- Wrapper: `div[data-automation-id="formField-jobTitle"][data-fkit-id="workExperience-135--jobTitle"]`

### 2. Dropdown (single select)
- Trigger: `<button aria-haspopup="listbox" id="education-220--degree" class="css-cxd5x">Select One</button>`
- Hidden value: `<input type="text" class="css-77hcv" value="dc8bf794766110882cd17d81ea3c3f28">`
- Caret icon: `<span class="menu-icon"><svg class="wd-icon-caret-down-small">`
- On click: renders popup with `data-automation-id="responsiveMonikerPrompt"` containing `role="listbox"` > `role="option"` items

### 3. Multiselect search (selectinput)
- Container: `div[data-uxi-widget-type="multiselect"]`
- Search input: `input[data-uxi-widget-type="selectinput"]` with `placeholder="Search"`
- Selected items: `ul[role="listbox"][data-automation-id="selectedItemList"]` > `li > div[role="option"]` pills
- Each pill: `div[data-automation-id="selectedItem"]` with delete charm (`data-automation-id="DELETE_charm"`)
- Prompt icon: `span[data-automation-id="promptSearchButton"]` (magnifying glass)

### 4. Date spinbutton
- Wrapper: `div[data-automation-id="dateInputWrapper"][role="group"]`
- Month: `input[role="spinbutton"][data-automation-id="dateSectionMonth-input"][aria-valuemin="1"][aria-valuemax="12"]`
- Year: `input[role="spinbutton"][data-automation-id="dateSectionYear-input"][aria-valuemin="1"][aria-valuemax="9999"]`
- Display: `div[data-automation-id="dateSectionMonth-display"]` (formatted "01")
- Calendar button: `div[data-automation-id="dateIcon"][role="button"]`

### 5. Checkbox
- `<input type="checkbox" aria-checked="true" class="css-18t536s">`
- Custom visual: `div.css-1lijdsm > span > svg.wd-icon-check-small`

### 6. File upload
- Drop zone: `div[data-automation-id="file-upload-drop-zone"]`
- Select button: `button[data-automation-id="select-files"]`
- Hidden input: `input[type="file"][data-automation-id="file-upload-input-ref"]`
- Uploaded item: `div[data-automation-id="file-upload-item"]` with filename, size, success status
- Delete: `button[data-automation-id="delete-file"]`

### 7. Repeating sections
- Groups: `div[role="group"][aria-labelledby="Work-Experience-1-panel"]`
- Headers: `h5#Work-Experience-1-panel`
- Delete: `button > svg.wd-icon-trash`
- Add: `button[data-automation-id="add-button"]` "Add Another"

### 8. Navigation
- Back: `button[data-automation-id="pageFooterBackButton"]`
- Next: `button[data-automation-id="pageFooterNextButton"]` "Save and Continue"
- Progress: `ol[data-automation-id="progressBar"]` with completed/active/inactive steps

## What DomHand Already Supports

| Feature | File | Status |
|---------|------|--------|
| `data-automation-id` selectors | `platforms/workday.py:117-149` | Defined but not fixture-tested |
| `data-uxi-widget-type="selectinput"` | `field_extractor.py:95` | Detected as `select` type |
| `role="spinbutton"` | `field_extractor.py:140` | Classified as `number` |
| Date section selectors | `platforms/workday.py:131-133` | Defined |
| File upload zone | `platforms/workday.py:137` | Selector defined |
| Add button | `platforms/workday.py:145` | Selector defined |
| Next/Back nav | `platforms/workday.py:140-141` | Selectors defined |

## Likely Failure Points

1. **Dropdown `<button aria-haspopup="listbox">`** — DomHand may not recognize this as a select field because there's no `role="combobox"` on the button itself. The `role="listbox"` only appears in a dynamically-rendered popup.

2. **Date spinbutton fill** — DomHand classifies `role="spinbutton"` as `number`, but the actual fill may need to set `aria-valuenow` via `page.evaluate()` rather than `page.fill()`, since these aren't standard text inputs.

3. **Multiselect pill management** — Selecting an option in the search popup adds a pill; DomHand needs to search → click option → verify pill appears. Clearing requires clicking the DELETE charm on each pill.

4. **Popup rendering on demand** — Dropdown options are NOT in the DOM until the trigger button is clicked. DomHand's `_discover_options` must click the trigger first, wait for popup, then scan options.

5. **Hidden value inputs** — Workday stores selected values in hidden `<input type="text" class="css-77hcv">` next to the trigger button. Reading back the selected value requires checking this hidden input, not the button text.

## Fixture Plan

Create `examples/toy-workday/index.html` covering:
- Work Experience section with text inputs + checkbox + date spinbuttons + textarea
- Education section with multiselect search (School) + dropdown (Degree) + multiselect pills (Field of Study)
- Languages section with dropdown selects
- Skills section with multiselect
- Resume upload with drag-drop
- Websites with repeating URL inputs + Add Another
- Progress bar (7 steps)
- Back/Save and Continue navigation

## Validation
```bash
uv run pytest tests/ci/ -k "workday" -v
```
