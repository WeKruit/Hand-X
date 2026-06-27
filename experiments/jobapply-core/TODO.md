# jobapply-core — TODO (deferred work)

The **deterministic single-page filler** (engine + Greenhouse/Lever/Ashby) is built and
verified live (see READMEs / commit). The items below are deferred.

## Multi-page / Workday (deferred — needs auth infra)
`ats_workday.py` + `run_wizard` are built and reach the live Create Account screen and
read the 7-step `progressBar` correctly, but cannot fill the form steps because:
- **Mandatory account creation + email verification.** Needs a real deliverable inbox
  (IMAP/API) the worker can poll, and per-(user,tenant) account storage. The single biggest
  blocker — accounts are per-tenant.
- **Per-tenant auth screens vary** (NVIDIA shows SSO-choice "Sign in with Google/Email"
  first; Visa adds a consent checkbox + cookie modal; Blue Origin has a honeypot field).
  Gate detection currently keys off the `progressBar` step name + auth aids; full handling
  needs per-tenant branches.
- **Possible CAPTCHA at account creation** (never exercised — read-only). Treat as HITL halt.
- The per-step widget routines (listbox-via-portal, segmented date, multiselect, file) are
  written from the design doc (`MULTIPAGE_DESIGN.md` §2) but **unverified past the auth wall.**
- Other wizard ATSes (iCIMS, Taleo, SuccessFactors) reuse `run_wizard` once Workday lands.

## Single-page widget gaps
- **Location geocomplete** (Greenhouse `candidate-location`, Ashby `_systemfield_location`):
  `el.fill` sets text but does NOT trigger the geocode suggestion menu (needs real
  per-char keystrokes) → no commit, lat/long not set. Spec in `WIDGETS_AND_REPEATERS.md`
  §2.1 (type → trusted Enter / pick suggestion). End-to-end (submit-accepted) unverified.
- **Date split-control** (employment/education history `start-date-month-{i}` + year text):
  routine spec'd (`WIDGETS_AND_REPEATERS.md` §2.2), reuses `_combobox`. Only appears inside
  repeaters → build with the repeater pass.
- **Signature** (Lever CC-305 typed-name + date): name-suffix → `full_name` / today
  deterministic rule. Draw-canvas → HITL (none seen in scope).
- **Ashby**: 1 textarea FAIL on the OpenAI posting (minor, uninvestigated).

## Experience / education repeaters (the big one — NOT pure schema-driven)
Verdict (`WIDGETS_AND_REPEATERS.md` §3): the schema can't enumerate rows. Needs a **hybrid
add-row loop** reusing `ghosthands/actions/domhand_fill_repeaters.py`: detect repeater off
the ignored `education`/`employment` schema flag → confirm `div.*--container` +
`button.add-another-button` → per profile entry: fill-before-add → per-row `_combobox` on
`#<field>--i` → read-back → STOP at N. Keep structurally separate from the flat FormField list.

## Polish
- MAP `_MAP_SYSTEM` consent rule auto-checks an *optional* marketing opt-in (Apollo) — tighten
  to only auto-select `required` consent options.
- `_screenshot` clip anchors on Greenhouse `#first_name`/`#email`; Lever's right-shifted layout
  clips the values out. Use a generic form bounding-box anchor for proof on non-Greenhouse boards.
- L3 escalation is gated off in proof sweeps (`allow_escalation=False`) because the
  browser_use.Agent teardown stops the CDP client; the re-attach (`session.connect()`) is wired
  but lightly tested under rapid multi-field escalation.
