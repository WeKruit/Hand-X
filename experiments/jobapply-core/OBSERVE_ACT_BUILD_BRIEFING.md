# observe_act — BUILD BRIEFING (read with OBSERVE_ACT_DESIGN.md)

**Branch:** `feat/observe-act-generic` (worktree `.claude/worktrees/observe-act-generic`). Evolution of the proven per-archetype filler — do NOT pollute `feat/deterministic-ats-filler`.

## Goal
ONE generic `observe_act(field, value)` fill primitive that works on ANY widget without relying on
renameable labels/aria/data-* — a deterministic orchestrator over **browser-use's OWN primitives**
(perception + action already exist; see OBSERVE_ACT_DESIGN.md §11 reuse-vs-build). Cost may go up a bit;
GENERIC + buildable + PROVEN is the bar. **PROVE on single-page ATS first** (no auth → no rate-limit):
**10 profiles × 50+ companies across Greenhouse / Lever / Ashby.** Fill-only, **NEVER submit**.

## REUSE (browser_use/, do NOT reinvent) — verified by research
- DELTA: serializer `previous_cached_state` → `is_new` (`dom/serializer/serializer.py:617`)
- elements + absolute coords + visibility + STRUCTURE-AGNOSTIC interactivity (event-listeners/cursor/role) — `dom/service.py`, `dom/serializer/clickable_elements.py`
- `dropdown_options` (native+ARIA+custom/Workday widgets) `tools/service.py:1555`; `select_dropdown` `:1604`
- TRUSTED click by index/coordinate (occlusion via elementFromPoint) `tools/service.py:540/584`
- char-by-char trusted `input` `:658`; `send_keys` `:1385`; `scroll` `:1280`
- set-of-marks see→click: `python_highlights.py:502` + `selector_map[backend_node_id]` `serializer.py:713`

## BUILD (net-new, thin — in experiments/jobapply-core/)
- `oa_perception.py` — thin wrapper: given a field located by VISIBLE LABEL, return its element node + the
  delta (new options after an open) + coords + visibility, via browser-use DomService (NOT new JS).
- `oa_action.py` — thin wrapper over browser-use tools: read_options, select_option, click(node/coord),
  type, press(Enter/ArrowDown), scroll. All TRUSTED CDP via browser-use.
- `oa_brain.py` — (1) classify field-nature from visible label+value (closed-list/free-text/date/bool/multi)
  → resolves Gap B; (2) typeahead SEARCH-LOOP (type partial → settle → re-read delta → variant retry,
  long-list type-first, Other/skip); (3) value-aware VLM verify + 3-way route (correct/empty/wrong).
- `oa_observe_act.py` — the §2 STATE MACHINE composing the above (S0→classify→intrinsic/closed/search/
  text→verify→recommit/revalue/cascade/multi→Other/escalate). Bounded retries; agent only last resort.
- `oa_singlepage.py` — a single-page runner that fills a Greenhouse/Lever/Ashby form field-by-field via
  observe_act. fill-only, never submit, screenshot at end.

## PROOF MATRIX (single-page, no auth)
- 10 profiles (varied: SWE/ML/ME/finance/PhD/etc., full experience+education+skills+EEO+free-text answers).
- 50+ companies across Greenhouse (`boards.greenhouse.io/{co}` / `boards-api.greenhouse.io`), Lever
  (`jobs.lever.co/{co}` / `api.lever.co/v0/postings/{co}`), Ashby (`jobs.ashbyhq.com/{co}` / posting API).
- Per (company × profile): run observe_act fill, record FILL-RATE (% required fields filled correctly,
  VLM-verified) + a screenshot + failure modes. NEVER submit.
- Report: fill-rate per ATS, the failure taxonomy (which widget shapes still fail), generic-ness verdict.

## HARD CONSTRAINTS
NEVER submit. No secrets in CLI args (env/.env). Run `.venv/bin/python` (anaconda lacks browser_use).
Verify OFFLINE first (imports, ruff, state machine over a captured DOM) before live. Throwaway data, no
real PII. browser_use is vendored in-tree (../../browser_use relative to repo root) — import + reuse it.
