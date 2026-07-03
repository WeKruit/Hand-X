# PLAN — drive COMPLETE-RATE 14% → 90%, in parallel with an auto-fix agent

Two tracks, run together (user: '先拉 complete-rate + 并行搭 auto-fix agent 骨架, 同时推进').

## Track A — close the 6 recurring required-field misses (drives complete-rate)

Ranked by (frequency × ease). Each fix is GENERIC (helps every platform), verified by a
screenshot + the `complete` verdict flipping, not by coverage %.

| # | miss | platforms | stage | approach | ease |
|---|---|---|---|---|---|
| A1 | Country / location dropdown empty | hibob | commit | searchable-select commit (already have `_s4_search`); ensure it runs for country | easy |
| A2 | Experience date `dd-mm-yyyy` not filled | hibob | action | format the profile date to the field's mask in the repeater row | easy |
| A3 | 2nd/3rd repeater row Add-after-save | breezy, rippling | action | re-scroll + re-visual-locate the Add after each save (partly done) | medium |
| A4 | localized required (Nom*, Requis) | teamtailor | match/audit | map + audit already label-driven; verify non-English labels flow through | medium |
| A5 | react-select rating verify-EMPTY | workable | commit | confirm the rating commit visually (value lives where DOM read-back can't see) | medium |
| A6 | resume behind a NATIVE picker (no DOM input) | hibob, bamboohr | action | needs a browser-use-level `Page.fileChooserOpened` hook — HARD, defer | hard |

Order: A1, A2 first (easy, hibob → 1.0 complete), then A4, A3, A5. A6 last (vendored change).

## Track B — auto-fix agent skeleton (the 'infinite iteration' the user asked about)

Today's loop is: capture (auto) → classify (auto) → rank (auto) → **FIX (manual, me)** →
re-measure (auto). Track B automates the FIX proposal so the loop can run without a human in
the diagnose step.

`auto_fix.py` (skeleton, this phase):
1. INGEST — read runs/failures/failures.jsonl + runs/l3_learn/corpus.jsonl + the per-field
   `trace` from result JSONs.
2. CATEGORIZE — map each failure to a STAGE from its trace:
   - `no-control` / `S1_LOCATE` fail → OBSERVE (locate)
   - `commit-failed` / `recommit EMPTY` / `text-type-refused` → COMMIT
   - `mark=-1` / apply-not-clicked / Add-click 0-fields → ACTION (affordance)
   - `visually_unanswered` / `missing_required` → COVERAGE (undiscovered)
3. CLUSTER + RANK — group by (stage, symptom), rank by frequency × burned-seconds.
4. BRIEF — per top cluster emit a structured fix-brief: {stage, count, evidence PNGs, the
   engine FILE + function most likely responsible, a suggested generic approach}.
5. (next layer) FIX-AGENT — a subagent takes a brief + the file, proposes a generic patch,
   self-checks, and the human/CI accepts. NOT built this phase — the brief is its input.

The brief is what makes the loop closable: it turns a pile of failures into 'here is the ONE
generic change that kills the biggest failure class, in this file, with this evidence.'
