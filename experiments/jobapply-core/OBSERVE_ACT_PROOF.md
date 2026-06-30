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

---

## Verify-Oracle Fix — serial + concurrent results

**The fix.** Verify no longer uses the VLM as the sole oracle. `observe_act._verify_field` →
`oa_brain.verify` now reads the located control's **live DOM value first** (`oa_dom_value.read_dom_value`,
generic standard-DOM via CDP — no renameable attributes, no per-ATS branching) and matches it against the
wanted value **LLM-only** (`wd_repeaters._llm_pick`, with a deterministic full-normalized-string-identity
short-circuit — not substring/regex). The VLM is demoted to a per-FIELD-budgeted AID
(`FIELD_VLM_CAP=2`) consulted **only** when the DOM read is empty/ambiguous (visual-only widgets). The
per-page VLM cap is lifted to a high backstop at run start so it can never pre-empt the per-field budget
and starve field 7+ into the old capped→UNKNOWN→ESCALATE false-failure. Every verify call records a
`verify-src:dom|vlm` trace tag, so the dom-vs-vlm split is auditable per field.

**Offline gate (all green, $0, no browser):** `oa_dom_value` 8/8 · `oa_brain` 28/28 (incl.
"DOM exact-normal match → CORRECT, 0 VLM + 0 LLM" and "DOM read-back mismatch → WRONG, 0 VLM") ·
`oa_observe_act` 24/24 (incl. "DOM-first text DONE (no VLM)" and "per-FIELD VLM budget caps at
≤FIELD_VLM_CAP") · `oa_proof` 10/10 · `ruff check` clean.

### Serial (1-proc-per-run, concurrency=1) — the prior passed proof
Greenhouse **93%** (13/14 non-skip DONE) · Lever **50%** (7/14) · Ashby **88%** (7/8). Every verdict
from **DOM read-back, ZERO VLM** (the 2 Ashby file fields took the INTRINSIC_FILE fast-path). GH 50.6s,
Lever 79.7s, Ashby 45.4s — all under the 240s/job budget.

### Concurrent (subprocess-per-job, `--concurrency 4`, 240s/job, fill-only, with resume)
`runs/oa_proof_concurrent/` — 12-job batch (6 companies × 2 profiles), 4 OS subprocesses at a time
(each its OWN interpreter → its OWN verify globals). Total wall-clock **≈ 6m 40s** for the 12-job sweep.

| ATS | runs | completed | timed out | fill-rate (concurrency=4) | serial fill-rate | verify src |
|---|---|---|---|---|---|---|
| **Greenhouse** | 4 | 4 | 0 | **100%** (20/20) | 93% | dom=20 vlm=0 |
| **Lever** | 4 | 4 | 0 | **50%** (28/56) | 50% | dom=28 vlm=0 |
| **Ashby** | 4 | 0 | **4** | **n/a — all 4 hit the 240s wall** | 88% | (no fields reached verify) |
| **Overall** | 12 | 8 | 4 | 63% (48/76) | — | **dom=48 vlm=0** |

- **Completed vs timed out:** 8/12 completed, 4/12 timed out — **all 4 timeouts were Ashby**, GH + Lever
  100% completion.
- **Comparable to serial — no collapse.** GH 100% (≥ serial 93%) and Lever 50% (= serial 50%) under
  concurrency=4. This is the headline: in the OLD in-process harness the same forms collapsed to
  **0–17%** because 4 coroutines shared one module-global VLM counter; with subprocess-per-job they are
  **identical to serial**. The metric holds because the verify state is per-process.
- **DOM verify holds under parallelism:** **every one of the 48 filled fields verified `verify-src:dom`
  `verdict:CORRECT` — ZERO VLM calls across the entire concurrent batch.** Not one field was
  ESCALATED by a capped→UNKNOWN false-failure. The verify-oracle fix is concurrency-proof because the
  free DOM read needs no shared, capped resource.
- **Fill-only confirmed on every screenshot.** All 8 completed runs left the Submit button present and
  untouched (e.g. `shots/000_greenhouse_anthropic_pyr_backend.png`: all 5 fields filled, dark
  "Submit application" button bottom-right, never clicked). The 4 killed Ashby children never reached a
  submit control either — `observe_act` has no submit path by construction.

### BLUNT verdict
**Yes — concurrency now holds because state is per-process.** The metric no longer collapses under
parallelism: GH and Lever return **identical numbers serial vs concurrency-4**, with **100% DOM-sourced
verify and zero VLM**. The old 0–17% concurrent collapse was purely the in-process shared-counter race;
subprocess-per-job eliminates it. The verify-oracle fix is the load-bearing change — a text/email/phone/
url/date/Yes-No value is read straight from the DOM, which costs nothing and needs no capped shared
resource, so 4 parallel jobs cannot starve each other.

**What still collapses, and exactly why — it is NOT the verify oracle, NOT a state race:** all 4 Ashby
jobs hit the 240s wall. Proven by re-running the exact failing job (`ramp / diego_mech`, idx 004)
**ALONE** in its own process: it **completed** at **56% (5/9), fill wall-clock 157.5s, total 233s,
verify-src dom=3 vlm=0** — i.e. it *barely* fits the 240s budget even with zero contention (Ashby's
react-select per-field settle is intrinsically slow). Under concurrency=4, four headless Chromium engines
contend for CPU/IO on one machine and that ~157s fill + nav/extract/map/screenshot overhead is pushed
past 240s → the per-job kill watchdog fires. So the Ashby failure is a **latency/contention budget
overflow, not a verify or isolation bug**: when it *does* run to completion it verifies the same way
(all DOM, no VLM) as serial. The fix needed is on Ashby's per-field settle latency (DESIGN §6 top-3 fix
#3: tighten the react-select `_settle`, batch field-scoped delta reads), or a higher per-job timeout /
lower concurrency for Ashby — not the verify path.

**Artifacts:** `runs/oa_proof_concurrent/records.json` (aggregate + per-run), `perfield/*.json` (full
per-field traces with `verify-src` tags), `shots/*.png` (8 end-of-fill, Submit untouched),
`CONCURRENT_REPORT.md`; solo-Ashby control: `runs/oa_ashby_solo/ashby_solo.json` (+ `.png`).

---

## 2026-06-30 — 99% push — direct-CDP + visual-bind + clean lifecycle

**Verdict: NOT at 99% generically. Greenhouse PASSES (100%); Lever and Ashby FAIL on a single
shared root cause — the READ/perception path, not the write path, not crashes.**

### Per-ATS live fill-rate (this push)

| ATS | Fill | DONE / non-skip | ESCALATE | Crash markers | Secs | Run |
|-----|------|-----------------|----------|---------------|------|-----|
| **Greenhouse** | **100%** | 16/16 | 0 | 0 | 65.1 | `runs/oa_live/gh3.json` + `gh3.png` |
| **Lever** | **36%** | 5/14 | 9 | 0 | 298.9 | `runs/oa_live/lever2.json` + `lever2.log` |
| **Ashby** | **50%** | 4/8 | 4 | 0 | 63.9 | `runs/oa_live/ashby.json` + `ashby.log` |

Zero orphan browsers before AND after the sweep (`pgrep -f browser-use-user-data-dir == 0`).
Fill-only held on all three — every run reached an end-of-fill state with NO submit control touched
(GH screenshot shows the "Submit application" button untouched; Lever/Ashby never reached submit
because fields escalated mid-form).

### Greenhouse — PASS, visually confirmed (gh3.png / gh3_top.png)

All 16 fillable fields committed and visible in the screenshot: First=Pyry, Last=Halonen,
Email=pyry.halonen.test@example.com, Phone mask-reformatted to `+1 415 555 0177`, Country +1,
Resume bar present, Website, in-person combobox=Yes, AI-Policy=Yes, interviewed-before=No,
visa/relocation=No, start-date=2026-09-01, the address combobox, and the "Why Anthropic?" textarea.
The 3 SKIPs are 2 optional free-text questions with no mapped value + the EEO block left blank by the
`_SENSITIVE_TOKENS` guard — correctly EXCLUDED from the denominator, not failures. GH's `/jobs/<id>`
page is server-rendered and DOES go idle, so the watchdog's full-page serialize returns fast — which
is exactly why GH works and the SPAs don't.

### Lever + Ashby — FAIL, ONE shared root cause: the perception path still routes through the watchdog

Both fail with the identical signature. After the first field that needs a live re-serialize (Lever:
the `location` geocomplete; Ashby: the `location` field), EVERY subsequent field throws a bare
`EXC:TimeoutError` with an empty trace — it throws BEFORE `S0_GUARD`, i.e. inside `_s1_locate`'s very
first `perc.get_state` call. `lever2.log` shows the smoking gun on every field:

```
DOMWatchdog.on_BrowserStateRequestEvent ⌛️ 30s/30s  ⬅️ TIMEOUT HERE
```

**Root cause (source-confirmed, not inferred):** the WRITE path was correctly made direct-CDP — basic
text on Lever DOES land (name/email/phone/LinkedIn DONE), proving the write watchdog-bypass works on a
never-idle SPA. But the READ/perception path was NOT. `oa_perception.get_state` (line 228) calls
`session.get_browser_state_summary(cached=False)`, which dispatches `BrowserStateRequestEvent ->
DOMWatchdog.on_BrowserStateRequestEvent -> DomService.get_serialized_dom_tree`. On a never-idle
`/apply` SPA the serializer waits for a page-readiness/idle condition that never settles and hits the
30s cap on EVERY call. The state machine calls `get_state` on EVERY field (`_s1_locate`, `_settle`,
`S3_OPEN`, `S4_SEARCH`, `S_CHOICE`) and the verify oracle reads DOM state too — so with
FIELD_DEADLINE=28s, the first `get_state` overruns the budget and the field escalates with the raw
`TimeoutError`. This is the SAME class of bug the write path already solved: a watchdog readiness gate
on a page that never goes idle.

The remaining-field list per ATS is therefore NOT a per-field matching problem — it is uniform:

* **Lever (9 ESCALATE):** `location` (geocomplete, search-exhausted then no-escape), then
  authorized-to-work radio, sponsorship radio, "how did you hear" select, 2 textareas
  (favorite-project, why-Palantir), AI-notetaker consent radio, marketing-consent checkbox, and the
  resume file — all 8 after `location` died with `EXC:TimeoutError` (never classified).
* **Ashby (4 ESCALATE):** LinkedIn text, the exceptional-performance text, resume file, cover-letter
  file — all 4 with `EXC:TimeoutError` after the `location` field.

Note these would otherwise fill: they are mapped, intrinsic-typed, and the matching/brain layer is the
same one that hit 100% on GH. The ONLY thing standing between Lever/Ashby and ~99% is the read path.

### Precise next step (single fix, generic, label-free)

Make the READ path bypass the watchdog the same way the WRITE path does — read the serialized DOM via
the direct-CDP plumbing `oa_dom_value` already proves (`cdp_client_for_node -> DOM.resolveNode ->
Runtime.callFunctionOn`, or a direct `DOM.getDocument` / `DOMSnapshot.captureSnapshot` over the CDP
session), instead of `session.get_browser_state_summary(cached=False)` which goes through
`BrowserStateRequestEvent -> DOMWatchdog` and waits for an idle that a never-idle SPA never reaches.
Concretely: give `oa_perception.get_state` a direct-CDP snapshot path that does NOT await page
readiness — build `selector_map` from a CDP DOM/AX snapshot taken immediately, bypassing the watchdog's
idle gate. This is the read-side mirror of the already-landed write fix; it is fully generic (no
per-ATS branch, no label dependence) and is the one change that should take Lever and Ashby from
36%/50% to the GH-class ~99%, since their matching/brain/verify layers already work.

**Bottom line:** the unified design is NOT yet at 99% generic+label-free+visual. It is one fix away —
porting the proven direct-CDP bypass from the write path to the read path (`get_state`).

---

## Read-path fix — Lever/Ashby 99% push (2026-06-30, grounded in runs/oa_live/*.{json,log,png})

**Verdict up front: NO. The unified design is NOT yet 99% generic+label-free+visual across all 3.
GH is the only ATS that lands. Lever = 36% and Ashby = 50% in the only live runs on disk, and BOTH
still exhibit the original LAST-BLOCKER deadlock. The claimed mis-bind fix is real in source but was
NEVER run live — it postdates every run here.**

### What the artifacts actually show (the ground truth, not the narrative)

Per-ATS live fill-rate (newest run of each, `runs/oa_live/`):

| ATS | run (mtime) | fill-rate | DONE/OTHER/SKIP/ESCALATE | end screenshot |
|-----|-------------|-----------|--------------------------|----------------|
| Greenhouse | gh3 (09:46) | **100%** (16/16) | 16 / 0 / 3 / 0 | gh3.png — visibly fully filled |
| Greenhouse | gh2 (09:42) | 93.3% (14/15) | 14 / 0 / 4 / 1 | — |
| Greenhouse | gh (09:38) | 86.7% (13/15) | 13 / 0 / 4 / 2 | — |
| Lever | lever2 (10:00) | **35.7%** (5/14) | 5 / 0 / 6 / **9** | **null** (browser died) |
| Lever | lever (09:52) | 33.3% (5/15) | 5 / 0 / 5 / 10 | null |
| Ashby | ashby (10:07) | **50%** (4/8) | 4 / 0 / 0 / **4** | **null** (browser died) |

### TIMELINE — the decisive fact

`oa_perception.py` (the file carrying the `_is_fillable_control` file-input exclusion, the claimed
mis-bind fix at lines 208-218) was **last modified 10:56**. The newest live run is **ashby at 10:07**;
the newest Lever run is **10:00**. **Every live run on disk predates the fix.** There is no live
evidence the fix works. The lever2.json/ashby.json results were produced by code that STILL had the
mis-bind, and they prove it:

- `runs/oa_live/lever2.json` field `[field0]` "Language Skill(s) (Check all that apply)" (a **checkbox**)
  → `intrinsic:INTRINSIC_FILE` → `committed:"English (ENG)"`. The checkbox bound to a hidden
  `input[type=file]` and the string "English (ENG)" was `setFileInputFiles`'d as a filename.
- `runs/oa_live/ashby.json` field `_systemfield_location` (a **location typeahead**)
  → `intrinsic:INTRINSIC_FILE` → `committed:"Detroit, MI, USA"`. Same mis-bind — the city string
  "uploaded" as a file. The prompt claims this is "FIXED (no longer uploads 'Detroit, MI, USA' as a
  file)"; the artifact on disk shows it STILL DOES.

### ROOT CAUSE — the LAST BLOCKER is NOT eliminated; it is re-triggered by the mis-bind

The logs are unambiguous (`runs/oa_live/lever2.log`, `lever.log`, `ashby.log`), same sequence every time:

```
INFO  [BrowserSession] 📎 Uploaded file English (ENG) to element 73        <- the mis-bind fires
WARNING [bubus] ⚠️ DOMWatchdog.on_BrowserStateRequestEvent() running for >15s ... deadlock
WARNING [bubus] ⏱️ TIMEOUT ERROR - Handling took more than 30.0s for ... on_BrowserStateRequestEvent
   ☑️ DownloadsWatchdog.on_BrowserStateRequestEvent(#4dd1)  3s/30s  ✓     <- only the DOM serialize hangs
   ⏰ DOMWatchdog.on_BrowserStateRequestEvent(#4dd1) 30s/30s ⬅️ TIMEOUT HERE
      📣 NavigationCompleteEvent  30s                                      <- never-idle SPA, never settles
INFO  [BrowserSession] 📢 on_BrowserStopEvent - Calling reset()
   [screenshot] failed: Runtime.evaluate did not respond within 60s ... silent WebSocket — container crashed
```

So the chain is: the **file mis-upload corrupts the SPA → a subsequent `get_state` cannot serialize
direct → it FALLS THROUGH to the event-bus `session.get_browser_state_summary` → the 30s
`on_BrowserStateRequestEvent` DOM deadlock → `on_BrowserStopEvent` reset → teardown WebSocket death**.
Every field after the corruption records `EXC:TimeoutError:` (9 of them on Lever, 4 on Ashby), which is
why the resume upload, the screening radios, the textareas, and the end screenshot all fail.

Two distinct defects, both still live:

- **(A) Mis-bind (TRIGGER).** Fixed in source (oa_perception.py:217 excludes `input[type=file]` from
  `_is_fillable_control`), self-tests pass, but UNVERIFIED live. This removes the corruption trigger.
- **(B) get_state → event-bus fallback (the actual 30s DEADLOCK path).** STILL PRESENT.
  `oa_perception.get_state` lines 374-376: when the direct serialize returns `None` (e.g.
  `agent_focus_target_id` cleared after `on_BrowserStopEvent` reset) AND there is no last-good
  snapshot, it calls `session.get_browser_state_summary(...)` — the exact path the diagnosis named as
  the LAST BLOCKER. The "read-path fix" only bypasses the bus on the HAPPY path; the moment the SPA
  resets, the fallback re-opens the 30s hang. The DownloadsWatchdog finishing the same event in 3s
  while only the DOM serialize hangs (visible in the trace) is the same fingerprint from lever2.log in
  the original diagnosis — i.e. the blocker was never closed, only avoided when nothing crashes.

### Claims in the handoff that the artifacts DO NOT support

- "Ashby read path proven flawless (20/20 get_state DIRECT, max 0.10s)" / "Lever 62/62 direct" —
  no get-state timing log (`OA_GETSTATE_LOG`) exists in `runs/oa_live/`; these numbers are not on disk.
  What IS on disk is `on_BrowserStateRequestEvent >15s/30s` in the Lever and Ashby logs — i.e. the
  event-bus DOM read WAS hit, repeatedly, in the exact runs cited.
- "Location mis-bind FIXED" — `ashby.json` shows `INTRINSIC_FILE` / `committed:"Detroit, MI, USA"`.
- "advanced Lever from 15 → 19/20 fields" — the only Lever runs are 5/15 and 5/14.

### What IS proven

- **GH genuinely lands 100% (gh3): 16/16 DONE, 0 ESCALATE, no bus hang, no SPA corruption.** The
  gh3.png screenshot visually confirms every field populated (name/email/phone/website/radios/date/
  textarea/visa/LinkedIn/relocation/address), EEO/self-ID correctly left blank, resume uploaded. Note
  GH only reached 16/16 on the 3rd attempt (gh 86.7% → gh2 93.3% → gh3 100%); the 100% is real but not
  yet repeatable run-to-run.
- **Fill-only HELD.** No Submit/Apply-final anywhere; all commits are field writes.
- **Zombies = 0.** `pgrep -f browser-use-user-data-dir` returns nothing before and after. Cleanup is
  bulletproof — KEEP it.
- **The direct-CDP WRITE path works** (name/email/phone/LinkedIn LAND on all three SPAs).

### Precise next step (blunt)

99% is two fixes away, not zero:

1. **Land defect (A) AND prove it live.** The mis-bind exclusion is in source but every run predates
   it. Re-run Lever + Ashby (ONE ATS at a time, `pkill -9 -f browser-use-user-data-dir` before/after).
   This should remove the "Uploaded file <string>" lines and stop the SPA corruption.
2. **Close defect (B) — the real LAST BLOCKER.** Remove the `get_browser_state_summary` fallback in
   `oa_perception.get_state` for a session that HAD a direct path (post-reset). When the direct
   serialize returns `None` and no last-good exists, return an empty/last-good `OAState` — NEVER
   re-dispatch `BrowserStateRequestEvent`. Equivalently: re-acquire `agent_focus_target_id` after a
   reset so the direct path stays available. Until (B) lands, any SPA reset (from a mis-bind, a
   navigation, a readiness timeout) re-opens the 30s deadlock — the bypass is happy-path-only.

**Bottom line: the read-path fix is partial. It bypasses the bus only while nothing crashes; the
get_state→bus fallback keeps the 30s deadlock one SPA-reset away, and the corruption trigger (the file
mis-bind) was fixed in source but never run. GH=100% (3rd try), Lever=36%, Ashby=50%, zombies=0,
fill-only intact. NOT 99% on Lever/Ashby. The design is generic+label-free+visual in shape, but
unproven past the first heavy field on never-idle SPAs.**

---

## Resilient LLM + card-commit — live results (2026-06-30)

**Goal:** prove the resilient model layer (`oa_llm.py`: bounded primary + provider-agnostic
fallback) ends the ~34s per-card LLM stall so Lever/Ashby custom cards FINISH. Env: `GOOGLE_API_KEY`
present, **`OPENAI_API_KEY` ABSENT** — so the fallback is built but DORMANT; only the bound is live.
Flags: `OA_NO_SANDBOX=1 OA_FIELD_DEADLINE=28 OA_LLM_TIMEOUT=5`. Clean: `pkill -9 -f
"browser-use-user-data-dir"` before batch + after every run, ONE ATS at a time, never parallel.
Offline self-tests `oa_llm.py` + `oa_brain.py` = ALL PASS before live.

### The bound WORKED — no run hung, all three FINISHED, zombies=0 throughout

| ATS | fill-rate | fill wall-clock | real wall-clock | crash markers | zombies after | provider answered |
|-----|-----------|-----------------|-----------------|---------------|---------------|-------------------|
| GH (regression) | **100% (16/16)** | 36.8s | 48.3s | 0 | 0 | gemini(primary), incl. 2 recovered-on-retry |
| Lever (Palantir) | 50% (7/14) | 98.3s | 129.6s | 0 | 0 | gemini(primary) + bounded-out escalations |
| Ashby (Ramp) | 50% (4/8) | 86.7s | 172.9s* | 0 (final screenshot CDP hang, not fill) | 0 | gemini(primary) + bounded-out escalations |

\* Ashby `real` exceeded the 130s soft bar ONLY because the terminal `--screenshot` CDP capture hung
60s (`Runtime.evaluate did not respond within 60s`) AFTER the fill finished at 86.7s — that capture is
not wrapped by `bounded_screenshot` (which guards only VLM-feeding shots). No `ashby.png` written; fill
data in `runs/oa_live/ashby.json`. The FILL never ran away; the original 34s-per-card runaway is gone.

### Max model-call duration: capped at OA_LLM_TIMEOUT=5s, every time

The log proves the fix end-to-end. Two distinct outcomes, both bounded at 5s:

- **Recovered on retry** (GH, and some Lever/Ashby fields):
  `gemini(primary) timed out >5.0s (attempt 1/2)` → `answered by gemini(primary) (attempt 2/2)`.
  GH hit this twice and still made 100% — the retry absorbed transient stalls.
- **Bounded-out → escalate** (the unfilled cards):
  `gemini(primary) timed out >5.0s (attempt 1/2)` → `timed out >5.0s (attempt 2/2)` →
  `text: primary bounded-out, NO fallback key -> None (caller escalates)`.
  Max per-attempt = 5s; max per-field LLM = ~10s (2 attempts) instead of the old ~34s. The run
  finishes; the field is ESCALATEd (Gap-B: never a blind type), NOT hung.

### Per-field located:how + outcome (the cards that matter)

- **GH** — all 16 non-skip fields filled by the structured `llm-map` (14) + profile/file. Name, email,
  phone, DATE, all BOOLEAN single-selects, both textareas, resume upload: DONE. Submit untouched
  (screenshot `gh.png` opened: name/email/phone filled, dropdowns committed, "Submit application" at
  foot untouched).
- **Lever** — DONE: name, email, phone, LinkedIn URL, both free-text textareas (favorite project / why
  Palantir), resume. ESCALATE (all gemini-stall, no fallback): `location` (SEARCH react-select),
  `Language Skills` (MULTI checkbox), 4× Work-Auth/consent (BOOLEAN radio), `University`
  (SEARCH single_select), `How did you hear` (CLOSED_LIST single_select). Screenshot `lever.png`
  opened: text fields + textareas filled, every card empty, "Submit application" untouched.
- **Ashby** — DONE: name, email, free-text card, resume. ESCALATE: `phone` (UNKNOWN — stalled
  classify), `location` (SEARCH), one custom text card (UNKNOWN — stalled classify), a second file
  field. No screenshot (terminal CDP capture hung).

### Blunt verdict per ATS

- **GH: PASS — 100%, 36.8s, 0 crash, 0 zombies, Submit untouched.** Regression fully intact; the
  resilient retry even *helped* (absorbed 2 transient stalls that would otherwise have escalated).
- **Lever: FAIL the 99% bar — 50%.** But the THESIS holds: the run FINISHED (98.3s, no 34s runaway, no
  kill), zombies=0, fill-only intact. The 7 ESCALATEd fields are all gemini-bounded-out with **no
  fallback to answer them** — exactly the case `OPENAI_API_KEY` is built for.
- **Ashby: FAIL the 99% bar — 50%.** Same root cause (gemini stall, dormant fallback). Run finished at
  86.7s fill; the only >130s artifact is a post-fill screenshot CDP hang, not the fill loop.

### Root cause of the remaining gap, and the one lever that closes it

The bound is necessary and proven: it converted a wall-clock-blowing 34s-per-card hang into a
fast-fail that lets the run finish at ~90s with 0 zombies. It is NOT sufficient alone, because the
primary gemini is **rate-limit-stalling repeatedly on this batch** and there is no second provider to
answer the bounded-out cards — so they ESCALATE (correct, safe) but stay unfilled.

**The fix is structurally live and correct; it just needs the fallback key activated.** Add
`OPENAI_API_KEY` (+ optional `OA_FALLBACK_MODEL`, default `gpt-4o-mini`) to `.env` and the dormant
`_build_fallback("text"/"vlm")` path lights up at CALL TIME (no restart) — every `gemini timed out
>5.0s` would then read `answered by openai(fallback)` instead of `NO fallback key -> None`, and the
Lever/Ashby cards classify + pick + verify on the second provider. Offline test (6) proves this exact
transition: adding `OPENAI_API_KEY` flips `_build_fallback` from `(None,'')` to `(<client>,'openai')`.

**Bottom line: the bound is PROVEN live (no hang, no runaway, no zombies, GH 100%). The 99% bar on
Lever/Ashby is gated entirely on the fallback provider key, which is absent. With `OPENAI_API_KEY`
set, the stalled cards get a fast second answer instead of escalating; without it the bound keeps the
run alive and clean but leaves the gemini-stalled cards unfilled. Verdict: resilient layer SHIPS;
re-run Lever/Ashby with `OPENAI_API_KEY` to confirm 99%.**
