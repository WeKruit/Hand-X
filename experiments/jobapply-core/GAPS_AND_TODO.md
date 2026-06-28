# ATS Filler — Gaps & TODO (handoff)

Single-page deterministic filler (Greenhouse/Lever/Ashby) is **merged to main** (PR #38): ~98% full-coverage, ~$0.0027/job (gemini-only), **fill-only — never submits**, verified across 6 profiles. This table is everything still open, for the Workday/multi-page thread to pick up.

**Owner:** Det = deterministic adapter routine · Agent = browser-use agent/escalation · Infra = harness/perf · Auth = Workday account+inbox · HITL = human-in-the-loop
**Priority:** P0 blocker · P1 high · P2 medium · P3 polish

| # | Area | Gap / TODO | Why it matters | Where | Owner | Pri | Status |
|---|------|-----------|----------------|-------|-------|-----|--------|
| **S1** | Agent safety | **Agent may OVERWRITE finished fields.** Freeze-guard locks *native* inputs, but custom widgets (react-select, calendar, radio-as-buttons) interact via div/button — freeze doesn't stop them. | An escalation/section agent that misreads a filled custom widget as empty can corrupt completed work. | `ats_engine.py` `_FREEZE_FILLED_JS`, `escalate`, `agent_fill_section` | Agent | **P0** | Partial — need **snapshot→verify→restore** of values (re-read filled fields after agent, restore any changed) |
| S2 | Agent safety | use_vision="auto" + single-field scope + max_steps=4 + freeze | Layered defense so agent rarely needs to re-touch | `ats_engine.py` | Agent | — | Done |
| **W1** | Workday/Auth | **Per-tenant account creation + EMAIL VERIFICATION** — needs a deliverable inbox (IMAP/API) the worker polls + per-(user,tenant) account store | The single biggest blocker; Workday gates the whole wizard behind it | `ats_workday.py`, `MULTIPAGE_DESIGN.md` | Auth | **P0** | Open (reaches the wall, AUTH_FAILED) |
| W2 | Workday/Auth | Per-tenant auth screens vary (SSO choice, consent checkbox, honeypot field) | Detection currently keys off progressBar + aids; needs per-tenant branches | `ats_workday.py` | Auth | P1 | Partial |
| W3 | Workday/Auth | CAPTCHA at account creation → HITL halt | Never exercised (read-only); treat as HITL | `ats_workday.py` | HITL | P1 | Untested |
| W4 | Workday | Per-step widget routines (listbox-via-portal, segmented date MM/DD/YYYY spinbuttons, multiselect, file) | Spec'd from design doc, **unverified past the auth wall** | `MULTIPAGE_DESIGN.md §2` | Det | P1 | Spec'd, unverified |
| W5 | Workday | Other wizard ATSes (iCIMS, Taleo, SuccessFactors) reuse `run_wizard` once Workday lands | Breadth after the pattern proves out | `ats_engine.py run_wizard` | Det | P3 | Future |
| **A1** | Agent take-over | **Education/experience repeaters** — off-schema rows + searchable closed-taxonomy comboboxes (school/degree/discipline) below the fold | Deterministic taxonomy-match fails; agent reasons it (B.S.→Bachelor's, ECE→Engineering) | `ats_engine.py agent_fill_section`, `ats_greenhouse.py fill_repeaters` | Agent | P1 | Working (agent, ~$0.02-0.05/job) |
| A2 | Agent take-over | Cache + verify the education agent result; consider deterministic add-row loop to drop cost | Education is the main cost driver | `agent_fill_section` | Det/Agent | P2 | Open |
| **D1** | Deterministic | **2 Ashby single_select residuals** (`1f170962…`, `57d2cab9…`) + 1 location + 1 checkbox variant | Long-tail custom-question widgets not covered | `ats_ashby.py` | Det | P2 | Open, uninvestigated |
| D2 | Deterministic | Lever location uses React native-setter (value sticks for fill-only) but does **not commit the geocode suggestion** (hidden `selectedLocation`) | Would FAIL on **submit** (we never submit) — only matters if submit is enabled | `ats_lever.py _location` | Det | P2 | Fill-only OK, submit-unverified |
| **D3** | Reachability | **GH orgs that redirect off-greenhouse** (stripe→stripe.com, databricks…) → BLOCKED "form not reachable" | Whole orgs are unfillable; not an agent gap (no GH form there) | `ats_greenhouse.py open_form` | Det | P1 | Open (needs redirect-follow / company-site adapter) |
| D4 | Deterministic | Cover-letter **required file** on a non-Greenhouse board | Skip-if-optional only wired for GH; required file elsewhere unhandled | adapters | Det | P3 | Edge |
| D5 | Deterministic | Date+textarea **adjacency cross-contamination** (one fill knocks the other) | Mostly fixed (date MM/DD/YYYY + textarea scroll-click-verify-retry) | `ats_ashby.py` | Det | P3 | Mostly fixed |
| **R1** | Read-back | **Wire the cheap-VLM visual-verify into deterministic read-back** as a fallback | Rescues genuine false-empties (DOM reads empty, visibly filled) WITHOUT paying for the agent — the bu-2-0 loop fix, reused | `vision_verify.py` → `ats_engine.py fill_with_ladder` | Det | P2 | Proposed, not done |
| **I1** | Infra | **browser-use leaks ~12 chromium/session;** `session.kill()` doesn't reap → conc>1 piles up (55 @ conc-5) → TIMEOUTs (looks like adapter fail) | Blocks parallel sweeps; only conc-1 + per-job reap is reliable | `sweep.py`, browser-use | Infra | **P1** | Workaround (conc-1+reap); fix = per-session PID tracking / fix kill |
| I2 | Infra | L3 agent CDP teardown drops the shared client; re-attach (`session.connect()`) wired but **lightly tested under rapid multi-field escalation** | Could break fields after an escalation | `ats_engine.py escalate` | Infra | P2 | Workaround |
| I3 | Infra | Discovery needs **per-org cap + reachability filter** (greenhouse-hosted absolute_url) — done ad-hoc, not a reusable tool | One big org (stripe) ate the quota + redirected | `runs/` scripts | Infra | P3 | Ad-hoc |
| A3 | HITL | **Draw-canvas signatures** → HITL/agent | None seen in scope; typed-name/date handled | — | HITL | P3 | Not seen |
| A4 | HITL | **CAPTCHA/reCAPTCHA** at submit → HITL | Never hit in fill-only (we don't submit) | — | HITL | P3 | N/A for fill-only |
| A5 | Agent take-over | Any **novel/unseen custom widget** → generic L3 escalate | Unknown unknowns; agent is the catch-all | `ats_engine.py escalate` | Agent | — | Ongoing |

## Quick reference
- **Cost:** deterministic ~$0.002/job · agent rescue ~$0.02–0.05/job (gemini) · zero bu-2-0.
- **Coverage now:** GH 98% · Lever 98% · Ashby 96% (deterministic, agent off).
- **Sacred invariant:** fill-only, never submits (single-page never clicks Submit; agents run with submit disabled).
- **Highest-value next:** S1 (snapshot-restore — make agent-takeover safe), W1 (Workday auth/inbox), D3 (GH redirect reachability), R1 (VLM read-back fallback).
