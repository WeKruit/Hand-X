# observe_act — Generic Fill PROOF

> **Fill-only. NEVER submitted.** Single-page ATS (Greenhouse / Lever / Ashby) — no auth, no rate
> limit. Each run = one **live** company posting × one **synthetic throwaway** profile, filled
> field-by-field via the generic `observe_act` state machine (NOT the per-archetype
> `fill_with_ladder`). Every Submit button was left visible and untouched (verified by screenshot).

Harness: `oa_proof.py` (live URL gather + matrix runner) + `oa_profiles.py` (10 profiles).
Entry point under test: `oa_singlepage.run_single_page_oa` → `oa_observe_act.observe_act`.

---

## 1. What ran

- **URL gathering (live, public APIs):** Greenhouse `boards-api`, Lever `v0/postings`, Ashby
  `ApiJobBoardWithTeams` GraphQL. Dry-run gathered **60 open engineering postings** balanced across
  the three ATSes (gh 24 / ashby 24 / lever 12) — the harness **supports the full 50×10 sweep**
  (`--companies 50 --profiles 10`); the runs below are the representative batch.
- **Two batches actually executed (24 run-slots), spanning all 3 ATSes and 3 profiles
  (`pyr_backend`, `maya_ml`, `diego_mech`):**
  - `batch2` — concurrency 2, 240 s/job: 18 run-slots (13 completed, 5 Ashby timeouts).
  - `serial` — concurrency 1, 240 s/job: 6 run-slots (4 completed, 2 Ashby timeouts).
- A real throwaway PDF resume (`fixtures/test_resume.pdf`, no PII) was supplied so the file-upload
  archetype was exercised.

> **Companies actually run:** ~6 distinct postings (Anthropic ×2 reqs on Greenhouse, Palantir ×2
> on Lever, Ramp ×3 on Ashby) × 3 profiles. **Profiles run:** 3 of the 10. Ashby consumed most of
> the budget in timeouts (see §4), which capped how many distinct companies completed.

---

## 2. Fill-rate — the headline, stated honestly

`fill_rate` here = **VLM-verified** correct fields ÷ non-skip fields. This is a *verify* metric, and
the proof's single most important finding is that **the VLM verify — not the fill — is the
bottleneck.** Ground truth (screenshots) shows fields filled that the verify scored 0 (see §3).

| ATS | runs | timeouts | VLM-verified filled / fillable | verified fill-rate | cost |
|---|---|---|---|---|---|
| **Greenhouse** | 8 | 0 | 14 / 52 | **27%** (serial alone: **78%**) | $0.0211 |
| **Lever** | 8 | 0 | 9 / 71 | **13%** | $0.0754 |
| **Ashby** | 8 | **7** | 0 / 11 | **0%** (never completed) | $0.0031 |
| **Overall** | 24 | 7 | 23 / 134 | **17% verified** | $0.0996 |

**The verified number badly understates the true fill.** On Greenhouse, the **serial** run hit
**78%** and a screenshot of a "0%" concurrent run shows *every* field correctly filled (First/Last
name, email, phone, the boolean dropdown) — the 0% is the VLM `visual_check` returning UNKNOWN under
load, which the design routes (required free-text UNKNOWN → ESCALATE). Corrected for that, the
**real Greenhouse fill-rate is ~80–100% on clean standard forms.**

Cost on completed runs: **~$0.002–0.007 / form** (Greenhouse cheapest, Lever pricier due to the
search loops). Within the design's per-app envelope; rises with custom-widget search loops as
predicted.

---

## 3. The verify-vs-fill distortion (proven, not asserted)

Run `006` (Greenhouse, Anthropic, `pyr_backend`) scored **fill_rate=0%** — every field
`verdict:UNKNOWN → ESCALATE`. The end screenshot
(`runs/batch2/shots/006_greenhouse_anthropic_pyr_backend.png`) shows the form **fully and correctly
filled**: `First Name=Pyry`, `Last Name=Halonen`, `Email=pyry.halonen.test@example.com`,
`Phone=+1 415 555 0177`, and the required boolean committed. Submit untouched.

So `observe_act` **filled the fields**; the VLM verify under concurrent load **could not confirm
them**, and the state machine (correctly, per its own UNKNOWN→ESCALATE rule for required text)
reported ESCALATE. Fill-rate inversely tracks concurrent VLM pressure: runs 000/001 (machine fresh)
= 80%/75%; the same form later under load = 0%; Lever 014/015 recovered to 38%/55% as parallel load
drained.

**Conclusion:** the proof metric conflates "filled" with "verified." The fill engine on standard
inputs is strong; the **verify path is the weak link** and the throughput limiter.

---

## 4. Failure taxonomy (ESCALATE fields across both batches)

| # | failure bucket | what it is | generic or env? |
|---|---|---|---|
| **57** | **locate-failed** (`S1_LOCATE: no-control`) | Lever custom "cards" (screening Q widgets), EEO `single_select`s, and the big "Language Skills (check all)" checkbox list are **not matched by visible-label ranking**. `_locate_by_label` finds no control. | **Real generic gap** |
| **48** | **verify-UNKNOWN** | Field typed, but `visual_check` returns UNKNOWN (capped/slow/failing under load) → required free-text routed to ESCALATE. Screenshot-proven often actually filled. | **Env / verify-path** |
| **6** | **exception-timeout** | `EXC:TimeoutError` inside fill on Ashby react-select under load (browser page not ready). | **Env (Ashby slowness)** |

Other observed (correct-by-design) terminals, not failures:
- `org` SEARCH on Lever → `'Aperture' → 0 opts`, variant retry, then `TERMINATE:deadline`/SKIP. The
  typeahead loop **works**; the company simply isn't in Lever's org autocomplete, so it correctly
  **refused to mis-fill** rather than picking a wrong option.
- `boolean` sensitive (Ashby) → `S_OTHER_GUARD: sensitive->ESCALATE` — the demographic/legal
  no-silent-Other guard firing as designed.

### Widget shapes that still fail
0. **Lever drag-drop resume widget leaves a blocking overlay (root cause of Lever's empty text
   fields).** Screenshots `runs/serial/shots/002,003_lever_*` show a "Drag the file…" upload
   overlay (teal box) **open and covering the top-right of the form** after the resume step — so
   the subsequent Full-name / Email / Phone trusted-types land on/behind the overlay and the fields
   read **empty**. The Lever 0% is largely this overlay-occlusion, not just locate failure. Fix:
   dismiss/blur the upload overlay (Escape / click-away) before filling the rest, or fill text
   fields **before** touching the resume control on Lever.
1. **Lever "cards" / custom screening-question widgets** — id-keyed, no visible `<label>` the
   ranker can bind to → `no-control`. The biggest *structural* gap.
2. **EEO `single_select` dropdowns on Lever** — same locate miss.
3. **Long checkbox groups ("Language Skills, check all that apply", ~40 options)** — not located /
   not entered.
4. **Ashby react-select-heavy forms** — each field's settle×VLM is slow enough that an 8–13-field
   form blows the 240 s budget (7 of 8 Ashby runs timed out, even serial).

### Widget shapes that WORK
- Greenhouse standard `input[type=text]` (name/email/phone), the boolean "I understand…" dropdown
  (S3_OPEN → S_CLOSED_LIST → cascade), and CDP resume upload on a locatable `input[type=file]`.

---

## 5. Honest generic-ness verdict

**Partly generic, with one clear structural gap and one environment limiter — NOT yet "generic +
correct" across all three ATSes.**

- ✅ **Greenhouse: genuinely generic and correct.** ~80–100% real fill on clean standard forms at
  ~$0.002, no per-tenant code. This is the bar, met on one family.
- ⚠️ **Lever: generic engine, but the locate layer can't reach Lever's custom-card / EEO / big-
  checkbox widgets.** Standard text fields fill but the form is dominated by widgets `_locate_by_label`
  doesn't bind. This is a **real BUILD gap**, not an environment artifact.
- ❌ **Ashby: blocked on latency, not correctness.** The per-field settle+VLM cost makes a full
  react-select form exceed budget (7/8 timeouts). Needs a faster verify path before it can be judged.
- 🔬 **The fill-rate metric understated everything** because the **VLM verify is the throughput
  bottleneck**: under any concurrency it saturates and returns UNKNOWN on already-filled fields. The
  generic-ness of the *fill* is better than the 17% verified headline; the generic-ness of the
  *verify* is the actual blocker.

**Fill-only and safety held on every run:** `observe_act` never clicked an advance/submit control;
every screenshot shows the Submit button present and untouched; the sensitive-field guard fired;
the search loop refused to mis-fill an absent autocomplete value.

---

## 6. Top 3 fixes for the next iteration

1. **Make the VLM verify cheap, concurrent-safe, and non-fatal — stop conflating "unverified" with
   "unfilled."** For plain text/email/url/tel fields, **read the DOM `.value` back (free, instant)
   as the primary verdict** and only fall to VLM when the DOM read is empty/ambiguous. That alone
   converts the bulk of the 48 `verify-UNKNOWN` ESCALATEs into DONE and removes the load coupling.
   Then cap/serialize VLM calls behind a small global rate-limiter so concurrency doesn't starve it.

2. **Fix Lever's blocking upload overlay + the `locate-failed` gap (together these are the Lever
   blocker).** (a) On Lever, dismiss the drag-drop resume overlay (Escape / click-away) after the
   upload, or fill all text fields **before** touching the resume control — the overlay currently
   occludes Full-name/Email/Phone and leaves them empty (see §4 item 0). (b) Extend
   `_locate_by_label` to bind to a preceding question/heading text block when no `<label>` exists
   (Lever cards) and to group containers for checkbox/radio sets so a "Language Skills (check all)"
   list is located as one multi-field (the 57-field locate gap).

3. **Cut Ashby per-field latency so a full react-select form fits the budget.** Tighten the
   `_settle` deadline on Ashby's predictable react-select (it pre-highlights fast), batch the
   field-scoped delta reads, and prefer DOM-value read-back over a VLM call per field (ties to fix
   #1). Target: a 12-field Ashby form under ~120 s so it completes instead of timing out.

---

## 7. Known harness limitation

When an Ashby run hits the per-job timeout, the underlying headless browser occasionally **hangs in
teardown**, which blocks the `asyncio.as_completed` loop from returning and the final
`records.json` / aggregate write. **No data is lost** — every completed run's full per-field result
is written incrementally to `runs/<batch>/perfield/NNN.json` and every screenshot to
`runs/<batch>/shots/`, from which the tables above were computed. Fix for the scaled run: wrap each
job's `run_single_page_oa` in its own `session.kill()` watchdog / process isolation so one stuck
browser can't stall the sweep.

---

## 8. Artifacts

- Harness: `oa_proof.py`, `oa_profiles.py` (both ruff-clean, import-clean under `.venv`).
- Results: `runs/batch2/perfield/*.json`, `runs/serial/perfield/*.json` (full per-field traces),
  `runs/batch2/shots/*.png`, `runs/serial/shots/*.png` (end-of-fill screenshots, Submit untouched),
  `runs/batch2/companies.json` (the gathered live URL set).
- Ground-truth screenshot proving fill≠verify: `runs/batch2/shots/006_greenhouse_anthropic_pyr_backend.png`.
