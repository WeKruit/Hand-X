---
status: investigating
trigger: "Debug why the browser-use Gemini agent doesn't properly observe that page fields are already filled, causing it to enter unnecessary manual recovery on Workday."
created: 2026-03-31T00:00:00Z
updated: 2026-03-31T00:00:00Z
---

## Current Focus

hypothesis: COMPOUND ROOT CAUSE -- three factors combine: (1) domhand_fill results are suppressed from read_state, so the agent only sees a terse summary in history, (2) the agent relies on screenshot + DOM for field state but Workday custom widgets use text nodes for values rather than input value attributes, making them look like labels not values, and (3) domhand_assess_state reports mismatch_count=1 and unresolved_required_count=4, which the agent interprets as "fields are incorrect" rather than "a few fields need attention"
test: Traced full pipeline from DOM -> serializer -> LLM prompt
expecting: Confirmed
next_action: Write up root cause analysis

## Symptoms

expected: After domhand_fill reports 47 fields already correct + 12 newly filled, the agent should recognize the page is filled and move on
actual: Agent enters 33-step manual recovery, scrolling/screenshotting/deleting/refilling fields that were already correctly filled
errors: No error per se - the agent makes wrong decisions because it can't see field values
reproduction: Run Workday application where domhand_fill successfully fills a form, observe agent behavior in subsequent steps
started: Ongoing behavior pattern

## Eliminated

- hypothesis: DOM serializer omits value attribute entirely
  evidence: DEFAULT_INCLUDE_ATTRIBUTES (dom/views.py:18-44) includes 'value'. _build_attributes_string (serializer.py:1193-1210) has special form element handling that reads value/valuetext from AX tree. Values ARE included for standard input elements.
  timestamp: 2026-03-31

- hypothesis: max_clickable_elements_length truncation cuts off field values
  evidence: No "truncated to" message in logs. Default is 40k chars. Total message is ~89k but that includes history + system prompt + DOM. The DOM portion fits within the 40k limit.
  timestamp: 2026-03-31

## Evidence

- timestamp: 2026-03-31
  checked: DEFAULT_INCLUDE_ATTRIBUTES in browser_use/dom/views.py:18-44
  found: 'value' IS in the default include list. Also includes 'checked', 'aria-checked', 'aria-valuenow', 'data-state'. The serializer is designed to show field values.
  implication: Standard input/select/textarea elements WILL show their values in the DOM serialization.

- timestamp: 2026-03-31
  checked: _build_attributes_string special form handling (serializer.py:1193-1210)
  found: For input/textarea/select, code explicitly checks AX tree for 'valuetext' and 'value' properties. This catches values set via JavaScript that don't update the HTML attribute. Works correctly for native HTML form elements.
  implication: The value pipeline is sound for standard elements. BUT Workday uses custom shadow DOM components where the "filled value" appears as a text node child, not as an input value attribute.

- timestamp: 2026-03-31
  checked: _READ_STATE_SUPPRESSED_TOOLS in message_manager/service.py:38
  found: domhand_fill AND domhand_assess_state are in the suppressed set. Their extracted_content is NOT placed into read_state. Only their long_term_memory goes into the history action_results.
  implication: The agent does NOT get a detailed read_state block showing which fields were filled and their values. It only gets the terse prose summary in history (e.g., "DomHand review: 12 verified, 47 already correct of 59 fields. Filled: Job Title, Company Name..."). The agent cannot tell WHICH specific fields were filled to what values.

- timestamp: 2026-03-31
  checked: Agent step history for Steps 10-14 from the log
  found: Step 10 calls domhand_fill which reports "12 filled, 0 DOM failures, 47 already correct, 2 skipped". Step 11 agent says "Success (partial)" because 2 required language fields were skipped. Step 12 calls domhand_assess_state which reports "unresolved_required_count=4, mismatched_count=1". Step 14 agent decides to "Delete the incorrect Education and Language entries."
  implication: The agent escalates from "2 skipped Language fields" to "delete everything and start over". The assess_state mismatch_count=1 amplifies this -- the agent interprets ONE mismatch as meaning the data is wrong and needs to be replaced.

- timestamp: 2026-03-31
  checked: Agent behavior from Step 14 onwards
  found: Agent deletes Education (UC Davis) and Language (Chinese) entries, then tries domhand_fill_repeaters which reports 0 fields (because the sections were pre-filled and expanding found nothing new). Agent then deletes Work Experience entries, manually fills Education one field at a time (School, Degree, Field of Study), and enters a 30+ step loop trying to fill dates and skills.
  implication: The "delete and refill" strategy is catastrophic. What domhand_fill did in one shot now takes 30+ steps and still fails.

- timestamp: 2026-03-31
  checked: How Workday renders filled form fields in the DOM
  found: Workday uses custom shadow DOM components. A "filled" combobox/text field often renders the selected value as a visible text node (e.g., "UC Davis" or "Chinese") inside a div or span, NOT as an input[value="UC Davis"]. The actual input element may be hidden or have a different structure. The AX tree captures the accessible name/value, but the visual rendering uses text nodes.
  implication: When the agent looks at the screenshot, it CAN see the values visually. When it reads the DOM serialization, it sees the text content near the interactive elements. But the agent does not have enough context to know "this text node IS the current value of that field" vs "this is a label". The terse domhand_fill summary doesn't map field names to values.

## Resolution

root_cause: |
  COMPOUND ROOT CAUSE -- three factors conspire:

  1. DOMHAND FILL RESULTS ARE INVISIBLE TO THE AGENT (Primary Factor):
     domhand_fill is in _READ_STATE_SUPPRESSED_TOOLS, so its detailed results
     (which field got which value) are NOT placed in read_state. The agent only
     sees a terse summary like "DomHand review: 12 verified, 47 already correct
     of 59 fields" in the history. It does NOT know which specific fields were
     filled or what values they contain. When it then looks at the page, it has
     no mapping from "field X = value Y" to verify against.

  2. ASSESS_STATE AMPLIFIES PARTIAL FAILURE INTO TOTAL FAILURE:
     domhand_fill says "2 Language fields skipped" (out of 59 total). The agent
     correctly calls domhand_assess_state, which reports mismatch_count=1 and
     unresolved_required_count=4. The agent interprets this as "the page has
     problems" and escalates to "delete incorrect entries" -- destroying 47
     correctly-filled fields to fix 2-4 issues.

  3. SCREENSHOT + DOM IS INSUFFICIENT FOR VALUE VERIFICATION:
     Workday's custom shadow DOM components render values as text nodes, not
     input value attributes. The agent sees visual values in screenshots but
     cannot programmatically distinguish "this text IS the field value" from
     "this text is a label." Without the domhand_fill value mapping, the agent
     is flying blind on what's correct vs what needs fixing.

  The combination means: domhand_fill fills 59 fields correctly, reports success,
  then the agent (a) can't see the fill details, (b) hears "4 issues" from
  assess_state, (c) can't verify individual fields, and (d) decides to delete
  everything and start over manually.

fix:
verification:
files_changed: []
