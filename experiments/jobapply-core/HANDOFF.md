# HANDOFF — deterministic schema-driven ATS filler

A generic engine that fills a job application by EXTRACTing the form's field schema,
mapping a profile onto it with ONE cheap structured LLM call, then driving each field
DETERMINISTICALLY (no agent loop). Single-page ATSes (Greenhouse / Lever / Ashby) are
built and **verified live**; Workday/multi-page is scaffolded and deferred (see `TODO.md`).

Cost: ~**$0.002 / application** (one `gemini-3-flash-preview` map call; deterministic fills
are $0). vs $0.09–0.27 for a full browser-use agent run.

## Adopt this work
```bash
git fetch origin
git checkout feat/deterministic-ats-filler
cd experiments/jobapply-core
uv venv --python 3.12 && source .venv/bin/activate
uv pip install browser-use python-dotenv certifi pydantic    # (or: pip install -e ../../.[dev])
playwright install chromium
echo "GOOGLE_API_KEY=sk-..." > .env
# run it (fill-only, never submits):
python greenhouse_fill.py \
  --job-url https://job-boards.greenhouse.io/anthropic/jobs/4461450008 \
  --profile fixtures/rich_profile.json --headless --screenshot /tmp/out.png
```
The CLI auto-picks the adapter by URL host (job-boards.greenhouse.io / boards.greenhouse.io,
jobs.lever.co, jobs.ashbyhq.com, *.myworkdayjobs.com).

## Files
| File | Role |
|------|------|
| `ats_engine.py` | **Generic engine** — `ATSAdapter` contract, the ONE structured map call (`map_fields`), per-field `fill_with_ladder` (L1 fill → L2 re-query+retry → L3 single-field browser-use Agent), read-back verify, per-field tier+cost instrumentation, `form_present` pre-flight, full-page screenshots. `run()` dispatches single-page vs `run_wizard`. |
| `greenhouse_schema.py` | Greenhouse extractor — `boards-api` schema → field plan. Reads ALL three blocks: `questions` + `location_questions` + `demographic_questions` (EEO). |
| `ats_greenhouse.py` | **GreenhouseAdapter** — id-locator, react-select combobox, checkbox-list, iframe-embed drill, WAF pre-flight, location geocomplete (best-effort). |
| `ats_lever.py` | **LeverAdapter** — live-DOM scrape (no schema API), `name`-keyed fills, native selects. |
| `ats_ashby.py` | **AshbyAdapter** — `non-user-graphql` schema, `data-field-path` locator, Yes/No buttons (read via `_active` class), labeled-checkbox multi-select. |
| `ats_workday.py` | **WorkdayAdapter** (multi_page, DEFERRED) — reaches the live Create Account wall + reads the 7-step `progressBar`; halts `AUTH_FAILED` (needs account+inbox infra). See `MULTIPAGE_DESIGN.md`. |
| `greenhouse_fill.py` | CLI / adapter registry. |
| `fixtures/rich_profile.json` | A full "user-layer" test profile (visa, zip, gender, ethnicity, …). |
| `MULTIPAGE_DESIGN.md` / `WIDGETS_AND_REPEATERS.md` / `TODO.md` | Design + verified widget specs + deferred-work list. |

## Architecture (第一性原理: invariant engine, variant adapter)
Implement a new ATS = one `ATSAdapter` subclass with 5 methods:
`extract(url, profile) -> (title, [FormField])` · `open_form(session, page) -> page` ·
`locate(page, field)` · `fill(session, page, field, value, resume) -> bool` ·
`read_back(session, page, field, value) -> bool`. The engine does the rest (map, ladder,
verify, instrument). `FormField.source ∈ {standard, select, input_text, open_ended, file, skip}`;
`needs_map` (select/input_text/open_ended) go through the one LLM call.

## Verified state (live, fill-only, never submitted)
- **Greenhouse**: full form incl. EEO + Location + checkboxes from the rich profile; 34-job
  breadth = 0 option-validity violations; iframe-embed + WAF cases handled. (twilio L1=18/19.)
- **Lever**: L1 16/17 (only the location typeahead remains).
- **Ashby**: L1 10/12 (only the location geocomplete + 1 textarea remain).

## Gotchas (hard-won — don't re-learn these)
- `Element.evaluate("()=>this.checked")` returns the Python **string** `"True"/"False"` —
  `bool("False")` is True. Use a string sentinel (`"()=>this.checked?'C':'U'"`), never `bool()`.
- react-select option polling must be **field-scoped** (`[id^="react-select-{name}-option"]`);
  a bare `[role=option]` also matches the phone widget's 244 country `<li>`s.
- Greenhouse EEO + Location live in **separate schema blocks**, not `questions`.
- `multi_value_multi_select` often renders as a **checkbox list** (match by option value-id).
- After a FAIL, do NOT re-acquire `page` via `must_get_current_page()` (it can latch a stray
  `about:blank`); only re-acquire after a real L3 escalation.
- Run proof sweeps with `allow_escalation=False` so the L3 agent doesn't tear down the CDP session.

## Where to continue → see `TODO.md`
Highest-value next: experience/education **repeater** hybrid (reuse
`ghosthands/actions/domhand_fill_repeaters.py`); then date/signature routines; then the
location geocomplete commit. Multi-page/Workday needs account+inbox infra (separate track).
