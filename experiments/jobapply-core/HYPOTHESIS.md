# HYPOTHESIS.md — the standing design contract (do NOT re-derive; extend)

Written 2026-07-01 after the nvidia deep-debug session + feasibility discussion.
Every future session: read this FIRST, work FROM it. Rebuilding something listed here = bug.

## 1. Core hypothesis (agreed, evidence-backed)

**"visuals + DOM + CDP can always resolve the filling"** holds for ACTING, not for KNOWING/DECIDING:

- **ACT layer is complete.** Trusted CDP input (mouse coords + key events) = human hands; screenshots = human
  eyes. Anything a human can fill, these primitives drive. Live evidence: an entire debug day produced ZERO
  act failures — every single bug was a read/verify bug. When a fill "fails", suspect the READ first.
- **READ layer is where all bugs live.** Session tally of silent-wrong reads found in mature code: the
  `bool(evaluate)` footgun, `selectedItem`-vs-`multiSelectPill` blindness, browser_use argless-braced
  `page.evaluate` throwing in-page (DOM read '' for every field while identical JS over raw CDP read fine),
  pill mounting seconds after the click (single-shot delta + read_back window both missed), `visual_check`
  cache rubber-stamping an empty field as success. RULES (permanent):
  1. NEVER `contextlib.suppress` a committed-value read — print the exception.
  2. Poll, don't single-read (commits lag clicks by seconds).
  3. `use_cache=False` on EVERY post-write visual verify.
  4. `page.evaluate` = ARGS-form only for braced multi-statement JS (`"(sel) => {...}", sel`).
  5. Commit marker must be the widget's native signal (pill-count delta), never wrapper text.
- **SEMANTIC gaps are policy, not capability.** No 'Mobile' in the tenant's phone list, no 'LinkedIn' under
  their sources, skills taxonomy = suggestion-only: NO amount of DOM/vision/CDP fixes these — a human can't
  commit an absent option either. Resolution = judgment: closest-available (LLM pick), reasonable default
  ("worked here before?" → No), or skip + residual. CARVE-OUT: attestation/legal fields (export control,
  clearance, visa, disability, veteran, criminal, salary, "I certify") get profile data or HITL — never a guess.
- **Hard limits (accepted, deferred):** anti-bot/WAF/CAPTCHA (browser-use cloud SDK later; desktop bridge on
  the USER's machine slashes fingerprint pressure), OS-native dialogs (routed around via DOM.setFileInputFiles),
  strict cross-origin iframes (HITL for now, OOPIF attach later), email verification (IMAP design exists).

## 2. The architecture (built, running — extend, don't rebuild)

```
L1 deterministic (DOM+CDP, ~$0.002/app)           ats_workday.py + wd_repeaters.py fixpoint loop
  → L2 cheap-LLM + cheap-VLM point decisions       _llm_pick / _llm_value_matches / visual_check
  → L3 generic agent escalation (residual-gated)   run_wizard residual_agent, browser-use Agent
  → residual report (fill-only, NEVER submit)      submit-guard + human review = final gate
ORACLES (in order): DOM read (free) → VLM read of the RENDERED value → Workday's own
validation-on-advance (server truth, catches whatever both misread).
```

- **Escalation caps the downside**: the only dead-end scenario is "cheap layer never generalizes and
  everything leaks to L3" — measured, not argued (see §5 gate metric).
- **Verify hierarchy per field**: act → DOM read → if miss/ambiguous → VLM confirm (that field only) →
  LLM judge (`_llm_value_matches`, exact-equality shortcut) → residual.
- **chosen-verify**: when the picker LLM mapped profile value → tenant option ('Mobile'→'Home Cellular'),
  read_back accepts committed==chosen. Verify the ACT, don't re-litigate the MAPPING.
- **Commit-by-node, never blind Enter**: read visible rows (deduped — parent+child read twice), exact→LLM
  pick the TEXT, trusted-click that node, pill-delta-poll. Hierarchical menus drill down ONE level
  (category → leaves). Virtualized lists scroll-hunt (pull last row into view, wait-until-changed;
  substring only PROPOSES, LLM confirms).
- **exclusive delta-correct**: ladder single-value fills REMOVE non-matching pills first (DELETE_charm);
  chip put() passes exclusive=False (never trims sibling skill pills). Autofill/suggested pills = free
  labor — respect, top-up, correct-only-when-provably-wrong.
- **Matching directive**: substring is NEVER match authority (false both directions); LLM is sole fuzzy
  authority; exact string equality is the only deterministic shortcut. Bound all LLM inputs (160 chars,
  60 options).

## 3. Title-ignorance (tenants rename/localize EVERYTHING)

- Field identity = `data-fkit-id` (structural, row-safe). Widget targeting/read roots = `_wsel` (fkit OR
  automation-id). NEVER heading/label text as identity.
- VLM prompts use the RUNTIME label read from the live DOM — renames/localization feed through automatically.
- Add-button anchor = structural (nearest fkit-holding ancestor's section prefix); heading keyword = fallback
  only for never-mounted sections.
- REMAINING keyword tables to retire → ONE cached cheap-LLM classifier (text, question) → answer:
  `KW`/`_HEAD_KW` heading→section, `_SEMANTIC_TEXT_KW` label→fuzzy-gate (better: gate by autocomplete
  machinery presence — pure structure). Scheduled after the sweep gate.

## 4. Provider economics (decided 2026-07-01)

- **OA_PRIMARY=openai** (prepaid credits = $0 marginal): gpt-5.4-nano for value reads/option picks/vision
  ($0.20/$1.25 per M; ~2710 img tokens/read ≈ $0.0006 nominal), gpt-5.4-mini for page-level mapping + L3
  agent. Gemini flash-lite = cross-vendor FALLBACK (uncorrelated errors — that's the point of reader #2).
- Live-verified: nano read 3/3 fields exact off a real screenshot (3.2s); nano picked 'Home Cellular' for
  'Mobile'. Nano is OCR-grade — sufficient for reads/picks; NOT for agent reasoning (mini there).
- Vision spend policy: DOM always; VLM only on post-write commits + DOM-miss/ambiguous (~4 fields/page ≈
  $0.003/page). Page-level vision reconcile (ONE screenshot → all label:value pairs → diff vs plan) queued
  as the batch upgrade.
- Every escalation is still 10-100x cheaper than agent-only ($0.5-2/app).

## 5. The gate metric (the ONLY number that decides feasibility)

**% of NEW Workday tenants reaching Review with ZERO code changes.** Sweep = `runs/wd_multi/seq10.sh`
(sequential, profile rotation 0-4, 25-min/tenant watchdog, pkill hygiene between tenants, fill-only,
email-verify auto-skip). Goal: ~20 different Workday jobs filled to Review. Sweep mode rules:
- On failure: NO mid-sweep CDP debugging. Tally status, rotate profile, move on.
- Between rounds: GENERIC fixes only (a fix must plausibly help ≥3 tenants).
- Rate-limit hygiene: one browser at a time, per-tenant account reuse (`creds_for` existing=True),
  rotate-on-signin-reject, throwaway mailinator emails, `WD_PASSWORD` env never argv.
- If marginal per-tenant debugging isn't shrinking after the gate → cut deterministic ambition, lean on L3.

## 6. Non-negotiables

- NEVER submit (submit-guard installed; Review is the finish line). Auto-submit = product-killing risk;
  human review gate stays until precision proven at scale.
- Fail direction must always be residual/escalation, NEVER silent wrong data.
- Live-test every CDP-action change on a real page + verify with independent raw CDP.
- Reuse before build: wd_one.py (auth/rotate/fetch), seq10.sh (sweep), wd_repeaters.py (fixpoint),
  oa_llm.py (resilient chain + openai_primary_llm), vision_verify.py (cached VLM reads), debug server
  (scratchpad wd_debug_server.py — hot-reloads wd_repeaters + ats_workday per command; ats_engine
  changes need restart).

## 7. Open gaps (owners = future sessions; all fail-safe direction)

| Gap | Bite | Fix path | Effort |
|---|---|---|---|
| Keyword tables (KW/_HEAD_KW/_SEMANTIC_TEXT_KW) | localized tenant → false-miss → escalation | shared cached LLM classify / structural gate | hours |
| Never-mounted section Add anchor | renamed heading + collapsed section | LLM heading classify (same helper) | hours |
| Row overflow (resume parse mounts 5 rows vs profile 2) | extra part-filled rows -> validation noise | reconcile row_overflow → delete-row affordance | half-day |
| OOPIF iframes (Greenhouse embeds) | embedded boards unreachable | CDP flatten attach; HITL interim | days |
| Page-level vision reconcile | per-field VLM latency adds up | 1 screenshot → all pairs → diff | half-day |
| browser_use argless-braced evaluate | wrapper throws in-page | args-form convention (done hot paths); patch vendored lib later | trivial/site |

## 8. The four capability categories (user's frame, 2026-07-02 — every problem files under one)

Any Workday fill problem decomposes into exactly these; a fix must be GENERIC within its category:

1. **OBSERVE** — see what a human sees: DOM read of rendered rows/values, vision when DOM is blind,
   scrolling as part of observation (virtualized lists: type-filter → scrollIntoView → trusted wheel →
   trusted ArrowDown focus-follow → [scrollbar drag, build-on-demand]). Human-input closure argument:
   a widget none of the five moves can scroll is one a human cannot scroll either.
2. **MATCH** — decide which observed value equals the wanted value: ONE normalizer both sides
   (eng.norm ∘ paren-strip), exact/bare first ($0), text-LLM over DOM texts for semantics,
   errored-LLM = unknown, never "no".
3. **COMMIT** — make the widget take it: trusted-click the matched node, pill-delta/native marker
   polled as the only truth, drill-down for category menus, exclusive removal for wrong pills,
   disarm on failure (click-outside + Escape).
4. **KNOW WHAT TO PUT IN** — the value side: profile map, reasonable defaults (worked-here→No),
   closest-available policy, attestation carve-outs, respect-autofill; residual + human review when
   genuinely unknowable.

Simulating human behavior (trusted input) + seeing values (observe) + context (LLM) spans all four —
that is why the class is closeable, not eternal.

## 9. Live finding 2026-07-02: the autodesk (wd1) scroll-only virtualizer — a real OBSERVE-tier boundary

countryPhoneCode committed=True DETERMINISTICALLY on paypal/hp/adobe/blueorigin (wd5/wd108 variants —
their virtualizer re-renders on scrollIntoView; bare-eq then commits the '(+1)' row). The autodesk wd1
variant is DIFFERENT and was tested LIVE against FOUR scroll mechanisms — ALL no-op (window frozen at
the A-Z first page, afghanistan→azerbaijan, n=30):
  1. type-to-filter  — input.value becomes 'united', list unchanged (this widget has no search filter)
  2. scrollIntoView(last row) — no re-render
  3. scrollTop += clientHeight on activeListContainer — container moved 0→4000 of 7500, rows FROZEN
  4. trusted CDP mouseWheel at container center — no re-render
  5. trusted ArrowDown batches — no re-render
activeListContainer geometry: clientHeight 151, scrollHeight 7500 (~240 rows windowed to 30).
CONCLUSION: this is a genuine DOM-OBSERVE limit, not a matcher/commit bug — the deterministic tier
cannot page this virtualizer. Correct generic handling per §8: it escalates to the VISION+agent tier
(L3) which sees the rendered strip via screenshot and scrolls via the agent's real browser actions.
The 4 scroll rungs are KEPT (fail-safe, cheap, and they DO carry the wd5-class variants); autodesk's
phone field is a recorded vision-tier case, NOT a deterministic gap to keep patching. Do not thrash a
6th scroll guess on it — the tier boundary is proven. Open (deferred): confirm the L3 agent commits it
via vision, and probe whether a per-window aria-posinset/setsize read exposes the full set another way.

## 10. Autodesk wd1 virtualizer SOLVED (2026-07-02) — type-ahead Enter, the generic commit

The wd1 countryPhoneCode (240 options, 151px clip) resisted SIX click-based commit mechanisms because
the option renders in the DOM but OUTSIDE the visible clip (US at y=737, clip 254-405) — elementFromPoint
returns null, scrolling repositions the virtualized node, so no click can ever land. BREAKTHROUGH: the
widget keeps a TYPE-AHEAD BUFFER — type the full value then Enter commits the matching option, even
though the visible list never filters. Wired as the FIRST commit in _multiselect (type value -> Enter ->
pill delta -> bare-eq/LLM verify the new pill matches the value). Result: autodesk My Info = 0 FAIL, ALL
fields L1 (How Did You Hear hierarchical, Country Phone Code virtualizer, Phone Device Type — all commit
via type-ahead Enter). This is GENERIC and cheaper than the scroll-hunt: type+Enter is now the primary
commit for every searchable multiselect; scroll-hunt + center-and-click remain as fallbacks. Also landed
this session: clip-aware _rows() (rendered!=visible!=clickable), synthetic scroll-event dispatch after
scrollTop (virtualizer re-renders on the event, not the property write), center-and-click primitive.
Every Workday widget variant met so far is now deterministically fillable.
