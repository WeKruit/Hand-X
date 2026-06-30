"""oa_observe_act — the deterministic STATE MACHINE (OBSERVE_ACT_DESIGN.md §2).

ONE generic ``observe_act(session, field)`` fill primitive that works on ANY widget
without relying on renameable labels/aria/data-*. It is a DETERMINISTIC orchestrator
over browser-use's OWN primitives — it composes the three foundation modules and adds
NOTHING that those already provide:

  * PERCEPTION  -> oa_perception  (get_state / locate_field / delta / node_* accessors)
  * ACTION      -> oa_action      (read_options / select_option / click_node / click_xy /
                                   type_text / press_key / upload_file / scroll — all TRUSTED CDP)
  * BRAIN       -> oa_brain       (classify_nature / query_variants / pick_option / verify)

The state machine is the §2 spine. Terminals: DONE | OTHER | SKIP | ESCALATE.
Per-field caps + a GLOBAL backstop (STEP_CAP / FIELD_DEADLINE) bound every loop.

HARD: fill-only. Nothing here submits a form. The only key helper sends individual keys
(Enter / ArrowDown / Backspace) the search-loop needs, never a submit control.

----------------------------------------------------------------------------------
SIGNATURE (per the BUILD task — the simpler dict form, reconciled with §2's FormField):
    async def observe_act(session, field: dict | Field) -> Outcome
    field = {"label": str, "value": str, "required": bool, "cardinality"?: "one"|"many",
             "resume"?: <path>, "llm"?: <ChatGoogle>}
§2 uses ``observe_act(session, page, FormField, value, resume, *, llm)``; this build is the
single-page proof harness, so we carry exactly the inputs a field needs in one dict and
read the located element straight off browser-use's selector_map (no separate ``page``
handle, no adapter ``locate`` — oa_perception.locate_field is the one structural read).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import oa_action as act
import oa_brain as brain
import oa_file_locate as filoc
import oa_perception as perc

# --------------------------------------------------------------------------- #
# Outcome terminals (§2). Mapped to the ladder in §6.5 by the runner.
# --------------------------------------------------------------------------- #
DONE = "DONE"  # field visibly holds the value (or a clearly-equivalent option)
OTHER = "OTHER"  # required, no exact match -> a genuine "Other"/"Prefer not" escape committed
SKIP = "SKIP"  # optional & blank / optional & unmatchable -> left blank (agent-repairable)
ESCALATE = "ESCALATE"  # required & unfillable deterministically -> caller's agent of last resort
Outcome = str

# PRODUCTION TIMEOUT POLICY (do not "fix" this into a process kill): when a REQUIRED field overruns
# its per-field budget (FIELD_DEADLINE / STEP_CAP), the guard returns ESCALATE so the field is handed
# to the agent of last resort and the rest of the form keeps filling — the engine NEVER kills the
# process to deal with a slow field. The hard subprocess kill in oa_proof.py is a DEV-SWEEP-ONLY
# convenience for one wedged headless browser; oa_proof.py --on-timeout escalate mirrors THIS
# per-field ESCALATE as the production-shaped behavior. See the policy block in oa_proof.run_matrix.

# --------------------------------------------------------------------------- #
# Per-field caps (§6.2). Per-axis + GLOBAL backstop.
# --------------------------------------------------------------------------- #
VARIANT_CAP = 3  # query variants (shared front + revalue, deduped) — comes from oa_brain too
SCROLL_CAP = 2  # off-screen / virtualized overlay reread
COMMIT_CAP = 2  # EMPTY re-commit, fresh verify key each time
REVALUE_CAP = 2  # WRONG_VALUE re-search, clear-first
CASCADE_CAP = 2  # sub-option recursion depth
MULTI_CAP = 8  # multi-value pick loop
STEP_CAP = 40  # GLOBAL per-field state entries
# GLOBAL per-field wall-clock (seconds). Env-tunable. Raised from 15->28: the bottleneck on a clean
# env is NOT a wedged page (those are bounded by the per-action CDP timeout + STEP_CAP) but the
# stacked latency of the cheap gemini classify + pick + verify calls — a single network spike on ONE
# of them used to blow the 15s budget and ESCALATE a field that would otherwise fill (observed: a
# different field times out each run, always "TERMINATE:deadline" right after a slow LLM call). 28s
# absorbs a spike while STEP_CAP=40 + the 4s per-CDP-action timeout still stop a genuinely stuck field.
FIELD_DEADLINE = float(os.environ.get("OA_FIELD_DEADLINE", "28.0"))
FIELD_VERIFY_CAP = 3  # per-FIELD verify attempts total (DOM read-back + VLM aids combined)
FIELD_VLM_CAP = 2  # per-FIELD VLM-aid sub-budget (DOM-first means the VLM is rarely needed)

# Settle timings (§3.5). Bounded poll, no fixed long sleeps.
# FIX 3 (speed): _settle used to re-serialize the WHOLE page every 0.12s for up to ~0.9s —
# 5-9 get_state/field on a heavy react-select page (~17s/field on Ashby). We KEEP the delta/visual
# signal but call it FAR fewer times: a coarser poll interval and a HARD cap on re-reads. A
# click-open menu mounts within one or two reads; we settle as soon as the delta is non-empty and
# stable, so a typical field now costs <=2-3 get_state.
_POLL_S = 0.30  # coarser than the old 0.12s — a menu still appears inside one interval
_SETTLE_READS_CAP = 3  # HARD cap on get_state re-reads inside one settle (was effectively ~5-8)
_SETTLE_STATIC_S = 0.6  # a click-open menu settles fast
_SETTLE_SEARCH_S = 0.9  # an async typeahead needs longer
_LIST_LONG = 12  # cluster >= this -> type-to-filter first (S_CLOSED_LIST long path)
_SCROLL_PX = 320  # one overlay page for the off-screen reread

# Labels that FORBID a silent "Other"/skip substitution (§S_OTHER_GUARD).
_SENSITIVE_TOKENS = frozenset(
    {
        "race",
        "ethnicity",
        "gender",
        "sex",
        "disability",
        "veteran",
        "hispanic",
        "latino",
        "authorized",
        "sponsorship",
        "sponsor",
        "visa",
        "18",
        "older",
        "age",
        "consent",
        "citizen",
        "eeo",
    }
)


def _is_sensitive(label: str) -> bool:
    toks = {t for t in perc._tokens(label)}  # reuse the same tokenizer the locator uses
    return bool(toks & _SENSITIVE_TOKENS)


# --------------------------------------------------------------------------- #
# Single-field context — the anti-spin budget the drafts lacked (§2 Ctx).
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    label: str
    value: str
    required: bool
    cardinality: str = "one"
    resume: str | None = None
    llm: Any = None

    # resolved during the run
    nature: str = ""
    node: Any = None  # the located EnhancedDOMTreeNode (the trigger/control)
    card: Any = None  # the enclosing question-card node (grouped-widget bind) -> scopes the choice group
    committed_text: str = ""  # EXACT option string we committed (for §6 cross-check)
    queries_tried: list[str] = dc_field(default_factory=list)
    ambiguous: bool = False

    # per-axis caps
    commit_tries: int = 0
    search_tries: int = 0
    revalue_tries: int = 0
    cascade_depth: int = 0
    scroll_reads: int = 0
    multi_done: int = 0

    # per-FIELD verify budget (§6.1 oracle fix): DOM read-back is primary + free; the VLM is a
    # budgeted AID. verify_used counts EVERY verify call (DOM+VLM); vlm_used counts only VLM aids.
    verify_used: int = 0
    vlm_used: int = 0

    # GLOBAL backstops
    steps: int = 0
    t0: float = dc_field(default_factory=time.monotonic)

    # trace for the runner's per-field report
    trace: list[str] = dc_field(default_factory=list)

    def guard(self) -> bool:
        """The single global guard run on every state entry. False -> TERMINATE."""
        self.steps += 1
        if self.steps > STEP_CAP:
            self.trace.append("TERMINATE:step_cap")
            return False
        if time.monotonic() - self.t0 > FIELD_DEADLINE:
            self.trace.append("TERMINATE:deadline")
            return False
        return True


# --------------------------------------------------------------------------- #
# Intrinsic HTML-type detection — DOM STANDARDS, read off the located node.
# These are W3C standards, not renameable; a tenant cannot ship a checkbox that is
# not a checkbox without breaking its own a11y (§4.1 Tier-A). We read them straight
# off browser-use's EnhancedDOMTreeNode (tag / type attr / role / ax role), never a
# data-automation-id / [for] / aria-controls hook.
# --------------------------------------------------------------------------- #
def _node_tag(node: Any) -> str:
    return (getattr(node, "node_name", "") or "").lower()


def _node_attr(node: Any, name: str) -> str:
    attrs = getattr(node, "attributes", None) or {}
    return (attrs.get(name) or "").lower()


def _node_role(node: Any) -> str:
    role = _node_attr(node, "role")
    if role:
        return role
    ax = getattr(node, "ax_node", None)
    return ((getattr(ax, "role", None) or "") if ax else "").lower()


def classify_intrinsic(node: Any) -> str:
    """Intrinsic nature from DOM standards, or '' (fall through to label-meaning).

    Returns one of: INTRINSIC_FILE | INTRINSIC_RADIO | INTRINSIC_CHECKBOX |
    INTRINSIC_SELECT | INTRINSIC_DATE | '' .
    """
    if node is None:
        return ""
    tag = _node_tag(node)
    typ = _node_attr(node, "type")
    role = _node_role(node)

    if tag == "input" and typ == "file":
        return "INTRINSIC_FILE"
    if tag == "select" or role == "listbox":
        return "INTRINSIC_SELECT"
    if (tag == "input" and typ == "radio") or role == "radio":
        return "INTRINSIC_RADIO"
    if (tag == "input" and typ == "checkbox") or role in ("checkbox", "switch"):
        return "INTRINSIC_CHECKBOX"
    if (tag == "input" and typ == "date") or role == "spinbutton":
        return "INTRINSIC_DATE"
    return ""


def _is_plain_text_editable(node: Any) -> bool:
    """POSITIVE free-text signal (§4.4): a plain text input/textarea/contenteditable that is
    NOT a combobox / autocomplete. The deterministic veto on a blind type."""
    if node is None:
        return False
    tag = _node_tag(node)
    role = _node_role(node)
    if role == "combobox":
        return False
    if _node_attr(node, "aria-autocomplete") not in ("", "none"):
        return False
    if tag == "textarea":
        return True
    if _node_attr(node, "contenteditable") in ("", "true"):
        return True
    if tag == "input":
        typ = _node_attr(node, "type") or "text"
        return typ in ("text", "email", "url", "tel", "search", "")
    return False


# --------------------------------------------------------------------------- #
# Settle (§3.5) — bounded poll over browser-use's own delta signal. No fixed sleep.
# Re-reads state every _POLL_S; settles on 2 identical consecutive delta reads OR the
# settle deadline. Returns the delta vs `before`.
# --------------------------------------------------------------------------- #
async def _settle(session: Any, before: perc.OAState, settle_s: float) -> list[perc.DeltaNode]:
    """Bounded poll over browser-use's delta signal — FIX 3: FAR fewer get_state, delta KEPT.

    Settles as soon as the delta is NON-EMPTY and stable across two coarse reads (a click-open menu
    appears within one or two reads), and is HARD-capped at ``_SETTLE_READS_CAP`` re-reads so a heavy
    react-select page can never spend 5-9 full-page serializes on one field. The delta/visual signal
    is unchanged — we just consult it a couple of times, not a dozen."""
    deadline = time.monotonic() + settle_s
    prev_ids: tuple[int, ...] | None = None
    last: list[perc.DeltaNode] = []
    reads = 0
    while True:
        await asyncio.sleep(_POLL_S)
        # forward ``before`` as the serializer's previous_cached_state so its own is_new delta signal
        # is preserved across the click/type that opened the menu (and the read stays bounded/fast).
        after = await perc.get_state(session, previous=before)
        reads += 1
        last = perc.delta(before, after)
        ids = tuple(d.backend_node_id for d in last)
        # settle on a NON-EMPTY stable delta (the menu mounted and held) — the common fast exit.
        if ids and prev_ids is not None and ids == prev_ids:
            return last
        prev_ids = ids
        if reads >= _SETTLE_READS_CAP or time.monotonic() >= deadline:
            return last


def _option_texts(nodes: list[perc.DeltaNode]) -> list[str]:
    """The human option labels from a delta cluster, committed-pill rows excluded.
    Pills carry a 'press delete to clear value' / selectedItem state which node_option_text
    already strips; here we drop empties and de-dupe, preserving order (top-to-bottom)."""
    seen: set[str] = set()
    out: list[str] = []
    for d in nodes:
        t = (d.text or "").strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _node_for_option(nodes: list[perc.DeltaNode], text: str) -> Any | None:
    want = perc._tokens(text)
    for d in nodes:
        if (d.text or "").strip().lower() == (text or "").strip().lower():
            return d.node
    # fall back to token-equality (handles a stripped suffix vs the picked string)
    for d in nodes:
        if perc._tokens(d.text) == want and want:
            return d.node
    return None


# --------------------------------------------------------------------------- #
# THE STATE MACHINE — observe_act (§2). Public entry.
# --------------------------------------------------------------------------- #
async def observe_act(session: Any, field: dict[str, Any] | Ctx) -> Outcome:
    """Fill ONE form field generically. Returns DONE | OTHER | SKIP | ESCALATE.

    `field` = {label, value, required, cardinality?, resume?, llm?}.
    NEVER submits. Bounded by per-axis caps + STEP_CAP / FIELD_DEADLINE.
    """
    ctx = (
        field
        if isinstance(field, Ctx)
        else Ctx(
            label=str(field.get("label", "")),
            value="" if field.get("value") is None else str(field.get("value")),
            required=bool(field.get("required", False)),
            cardinality=str(field.get("cardinality", "one")),
            resume=field.get("resume"),
            llm=field.get("llm"),
        )
    )
    out = await _s0_guard(session, ctx)
    if not isinstance(field, Ctx):
        # surface the trace on the dict so the runner can record per-field detail
        field["_trace"] = ctx.trace
        field["_nature"] = ctx.nature
        field["_committed"] = ctx.committed_text
    return out


# ---- S0_GUARD ----
async def _s0_guard(session: Any, ctx: Ctx) -> Outcome:
    ctx.trace.append("S0_GUARD")
    # A file field carries its payload in ``resume`` (the value may be blank) — it is NOT a blank
    # field, so the blank->SKIP gate must not swallow it before the global file path runs.
    if not ctx.value.strip() and not ctx.resume:
        ctx.trace.append("blank->SKIP")
        return SKIP
    return await _s1_locate(session, ctx)


# ---- S1_LOCATE ----
async def _s1_locate(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S1_LOCATE")
    # FIX 3 (speed): ONE full-page get_state here; it is forwarded to classify so a normal field
    # never re-serializes just to classify the control it already located.
    state = await perc.get_state(session)

    # FIX 2 (global file path): a resume/CV/cover-letter field's input[type=file] is usually
    # HIDDEN / zero-box, so the generic label ranker returns no-control. Route it to the dedicated
    # GLOBAL file path BEFORE generic locate — it scans ALL file inputs (incl. hidden) and matches
    # by tokens via the LLM picker. The file signal is the resume path the runner attaches to a
    # file-source field; GENERIC (no per-ATS string, no per-ATS branch).
    if ctx.resume:
        fout = await _s_file_global(session, ctx, state)
        if fout is not None:
            return fout
        # else: no file input on the page -> fall through to generic locate (mis-tagged field).

    # FIX 1: tiered locate — STRUCTURE first, VISUAL PROXIMITY aid, GROUPED-WIDGET card-heading bind,
    # VLM disambiguate. Binds an unlabeled card input (Lever) the way a human does — by the question
    # text sitting near it; a non-text card (radio/checkbox/select/textarea) binds via its card heading.
    node, how, card = await perc.locate_field_tiered(
        state, ctx.label, vlm_pick=_make_vlm_pick(session, ctx), marks_pick=_make_marks_pick(session, ctx)
    )
    # FIX (below-the-fold): a question lower on the page may have its card marked not-visible, so the
    # locate sees no control. Scroll the page down one viewport and re-locate, BOUNDED — a question
    # must not be missed just for sitting below the fold, but we never loop forever chasing one.
    # SCROLL-LOCATE is OFF by default: on a heavy SPA (Lever/Ashby) a per-field scroll + FULL
    # get_state re-serialize, repeated for every unbound card, piles enough CDP load to make headless
    # Chrome go unresponsive (silent WebSocket) — i.e. it caused the very loop/crash it was meant to
    # avoid. Gate it behind OA_SCROLL_LOCATE=1 until below-fold binding is done from the full DOM
    # (browser-use's trusted click auto-scrolls a node into view, so a below-fold card can be bound
    # WITHOUT a heavy re-serialize). Default-off keeps the engine fast and crash-free.
    if node is None and os.environ.get("OA_SCROLL_LOCATE") == "1":
        node, how, card, state = await _scroll_locate(session, ctx, state)
    if node is None:
        ctx.trace.append("no-control")
        return ESCALATE if ctx.required else SKIP
    ctx.node = node
    ctx.card = card
    ctx.trace.append(f"located:{how}")
    # label-collision (repeaters with two "Degree"): a structural tie forces a value-verify later
    # (§6 fast-path off). Only the structure tier can produce a true accessible-name collision.
    if how == "structure":
        ranked = perc.locate_field_ranked(state, ctx.label)
        if len(ranked) >= 2 and abs(ranked[0][1] - ranked[1][1]) < 1e-9:
            ctx.ambiguous = True
            ctx.trace.append("ambiguous-label")
    return await _s2_classify(session, ctx, state)


async def _scroll_locate(session: Any, ctx: Ctx, state: perc.OAState) -> tuple[Any, str, Any, perc.OAState]:
    """BOUNDED scroll-into-view re-locate for a card below the fold (FIX point 3).

    A question lower on the page can have its card marked not-visible by browser-use, so the first
    locate returns no control. We scroll the PAGE down one viewport (reusing ``act.scroll`` — the
    same trusted CDP wheel the option-reread uses) and re-serialize, at most ``SCROLL_CAP`` times,
    re-running the full tiered locate each step. Returns the first non-None bind plus the fresh state
    (so the caller classifies on the post-scroll DOM), or the last (None, "", None, state). Hard-capped
    — never a scroll loop; if the card never appears within the bound we give up and the field
    escalates/skips as before."""
    how = ""
    card = None
    node = None
    while ctx.scroll_reads < SCROLL_CAP:
        ctx.scroll_reads += 1
        await act.scroll(session, None, _SCROLL_PX)
        state = await perc.get_state(session)
        node, how, card = await perc.locate_field_tiered(
            state, ctx.label, vlm_pick=_make_vlm_pick(session, ctx), marks_pick=_make_marks_pick(session, ctx)
        )
        ctx.trace.append(f"scroll-locate#{ctx.scroll_reads}:{'hit' if node is not None else 'miss'}")
        if node is not None:
            return (node, how, card, state)
    return (None, how, card, state)


def _make_vlm_pick(session: Any, ctx: Ctx) -> Any:
    """A bounded VLM disambiguation callback for locate Tier-3 (a genuine spatial tie only).

    Returns an async ``(label, [candidate_nodes]) -> node | None`` that asks the per-field VLM
    budget to pick which candidate control belongs to the question. AID only — it spends from the
    SAME per-field VLM sub-budget the verify oracle uses (``FIELD_VLM_CAP``) so locate + verify
    together never over-spend the VLM on one field. Returns None (caller falls back) when the
    budget is spent or no VLM/llm is available."""

    async def _pick(label: str, cands: list[Any]) -> Any:
        if ctx.vlm_used >= FIELD_VLM_CAP or not cands:
            return None
        before_n = _vv_calls()
        chosen = None
        try:
            chosen = await brain.pick_control_by_vision(session, label, cands, llm=ctx.llm)
        except Exception:
            chosen = None
        spent = max(0, _vv_calls() - before_n)
        if spent:
            ctx.vlm_used += spent
            ctx.trace.append("locate-vlm-aid")
        return chosen

    return _pick


def _make_marks_pick(session: Any, ctx: Ctx) -> Any:
    """A bounded VISUAL SET-OF-MARKS bind callback for locate Tier-2d (a label-free non-text card).

    Returns an async ``(label, [candidate_nodes]) -> node | None`` that marks the candidate controls
    on the page screenshot (browser-use ``create_highlighted_screenshot``) and asks the cheap VLM
    which marked control is the answer for this question. AID only — it spends from the SAME per-field
    VLM sub-budget the verify oracle + Tier-3 use (``FIELD_VLM_CAP``), so locate + verify together
    never over-spend the VLM on one field. Returns None (caller falls back) when the budget is spent
    or no VLM/llm is available."""

    async def _pick(label: str, cands: list[Any]) -> Any:
        if ctx.vlm_used >= FIELD_VLM_CAP or not cands:
            return None
        before_n = _vv_calls()
        chosen = None
        try:
            chosen = await brain.pick_control_by_marks(session, label, cands, llm=ctx.llm)
        except Exception:
            chosen = None
        spent = max(0, _vv_calls() - before_n)
        if spent:
            ctx.vlm_used += spent
            ctx.trace.append("locate-marks-aid")
        return chosen

    return _pick


# ---- S2_CLASSIFY ----
async def _s2_classify(session: Any, ctx: Ctx, state: perc.OAState | None = None) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S2_CLASSIFY")
    intrinsic = classify_intrinsic(ctx.node)
    if intrinsic:
        ctx.nature = intrinsic
        ctx.trace.append(f"intrinsic:{intrinsic}")
        if intrinsic == "INTRINSIC_FILE":
            return await _s_file(session, ctx)
        if intrinsic in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
            # SNAPSHOT REUSE: the radio/checkbox options are STATIC siblings already present in the
            # locate snapshot (no click reveals them) — pass it through so _s_choice reads the group
            # from the SAME serialize, never a second full-page get_state per choice field.
            return await _s_choice(session, ctx, state)
        if intrinsic == "INTRINSIC_SELECT":
            return await _s_native(session, ctx)
        if intrinsic == "INTRINSIC_DATE":
            return await _s_date(session, ctx)

    # label-meaning nature (§4.2) — one cheap LLM call, deterministic overrides in code (§4.3).
    # MULTI mis-route fix: a comma in the value is a multi signal ONLY for a genuine multi-value
    # label (Skills/Languages/Technologies). A single 'Current location' value "San Francisco, CA"
    # carries a comma but is ONE value — gate value_is_list on is_multi_label so it never forces
    # MULTI (which would type "San Francisco" then "CA" as two pills). known_multi already carries
    # the runner's authoritative cardinality.
    multi_label = brain.is_multi_label(ctx.label) or ctx.cardinality == "many"
    hints = brain.ClassifyHints(
        known_multi=(ctx.cardinality == "many"),
        value_is_list=(multi_label and ("," in ctx.value or ";" in ctx.value)),
    )
    nature = await brain.classify_nature(ctx.label, ctx.value, hints, llm=ctx.llm)
    ctx.nature = nature
    ctx.trace.append(f"nature:{nature}")

    if nature == "DATE":
        return await _s_date(session, ctx)
    if nature == "BOOLEAN":
        # a yes/no value on a non-intrinsic control behaves like a closed list of {Yes,No}
        return await _s3_open(session, ctx)
    if nature == "CLOSED_LIST":
        return await _s3_open(session, ctx)
    if nature in ("SEARCH", "MULTI"):
        return await _s4_search(session, ctx)
    if nature == "FREE_TEXT":
        return await _s_text_guard(session, ctx)
    # UNKNOWN (Gap B): never type blindly. required -> escalate; optional -> skip.
    ctx.trace.append("UNKNOWN-nature")
    return ESCALATE if ctx.required else SKIP


# ---- S_FILE_GLOBAL (FIX 2: the dedicated file path, any ATS) ----
async def _s_file_global(session: Any, ctx: Ctx, state: perc.OAState) -> Outcome | None:
    """GLOBAL deterministic file upload — finds the file input even when it is HIDDEN / zero-box.

    Returns an Outcome (DONE/SKIP/ESCALATE) when this page has a file input to handle, or ``None``
    when there is NO file input at all (the resume-tagged field was mis-tagged -> caller falls back
    to generic locate). GENERIC, no per-ATS code:
      1. ``find_file_input`` scans ALL input[type=file] (incl. hidden) and picks the best by tokens.
      2. ``is_already_uploaded`` reads whether a file is already attached -> idempotent DONE.
      3. else ``act.upload_file`` (CDP setFileInputFiles — NO OS picker)."""
    node = await filoc.find_file_input(state, ctx.label, llm=ctx.llm)
    if node is None:
        return None  # no file input on the page -> not actually a file field here
    ctx.node = node
    ctx.nature = "INTRINSIC_FILE"
    ctx.trace.append("S_FILE_GLOBAL")
    path = ctx.resume or ctx.value
    if not path:
        return SKIP
    # idempotent: if a file is already attached / shown, do NOT re-upload (read-if-we've-uploaded).
    if await filoc.is_already_uploaded(session, node):
        ctx.trace.append("already-uploaded->DONE")
        ctx.committed_text = str(path)
        return DONE
    ok = await act.upload_file(session, node, str(path))  # CDP only, NO click (no OS picker)
    if not ok:
        ctx.trace.append("upload-failed")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = str(path)
    ctx.trace.append("uploaded")
    return DONE


# ---- S_FILE (intrinsic-located file input — falls back to the global locator if hidden) ----
async def _s_file(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_FILE")
    path = ctx.resume or ctx.value
    if not path:
        return SKIP
    # idempotent guard even on the intrinsic path (a re-run must not double-upload).
    if await filoc.is_already_uploaded(session, ctx.node):
        ctx.trace.append("already-uploaded->DONE")
        ctx.committed_text = str(path)
        return DONE
    ok = await act.upload_file(session, ctx.node, str(path))  # CDP only, NO click (no OS picker)
    if not ok:
        ctx.trace.append("upload-failed")
        return ESCALATE if ctx.required else SKIP
    # presence-only verify (the file input value is opaque to the VLM; CDP set is reliable)
    ctx.committed_text = str(path)
    return DONE


# ---- S_CHOICE (radio / checkbox; options already on screen) ----
async def _s_choice(session: Any, ctx: Ctx, state: perc.OAState | None = None) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_CHOICE")
    # The group's options are siblings already rendered. SNAPSHOT REUSE: prefer the locate snapshot
    # handed down from classify (the static radio/checkbox group is already in it) — only serialize
    # afresh if the caller had none (e.g. a re-entry). This removes a full-page get_state per choice
    # field, a primary cost on a heavy SPA.
    if state is None:
        state = await perc.get_state(session)
    group = _read_choice_group(state, ctx)
    if not group:
        # single standalone checkbox (consent / yes-no) — toggle on an affirmative value.
        if brain._norm_lower(ctx.value) in brain._BOOL_VALUES and brain._norm_lower(ctx.value) not in {
            "no",
            "false",
            "n",
            "decline",
        }:
            await act.click_node(session, ctx.node)
            return await _s_verify(session, ctx)
        ctx.trace.append("choice-no-group")
        return await _s_other_guard(session, ctx)
    texts = [t for t, _ in group]
    chosen = await brain.pick_option(ctx.value, texts, llm=ctx.llm)
    if not chosen:
        return await _s_other_guard(session, ctx)
    node = dict(group).get(chosen)
    if node is None:
        return await _s_other_guard(session, ctx)
    ctx.committed_text = chosen
    # Point ctx.node at the CHOSEN option control (it was the representative until now for a grouped
    # bind) so the verify oracle reads back the control we actually committed, not the group's first
    # option — the DOM read-back then checks the real selected radio/checkbox.
    ctx.node = node
    await act.click_node(session, node)  # TRUSTED click on the visible proxy / input
    return await _s_verify(session, ctx)


def _read_choice_group(state: perc.OAState, ctx: Ctx) -> list[tuple[str, Any]]:
    """The radio/checkbox options that belong to THIS question's group, scoped to its card.

    Structure-agnostic: a control whose intrinsic kind matches the trigger. When the field was bound
    via the grouped-widget tier (``ctx.card`` set), we scope the scan to that CARD's subtree so a
    multi-question page picks the RIGHT options for THIS question — the prior whole-page scan grouped
    by intrinsic-kind across ALL questions and so could pull another question's Yes/No into this group.
    Falls back to the whole page when no card is known (a single standalone toggle). Each option's
    label is its own visible text (a radio's name IS its option, e.g. 'Yes'). Returns [(option, node)].
    """
    want_kind = classify_intrinsic(ctx.node)
    # Scope to the card subtree (grouped bind) — reuse perception's own structural walk — else page.
    pool = perc._controls_in(ctx.card) if ctx.card is not None else list(state.selector_map.values())
    out: list[tuple[str, Any]] = []
    for node in pool:
        if not perc.node_is_visible(node):
            continue
        if classify_intrinsic(node) != want_kind:
            continue
        label = perc.node_label_text(node)
        if label and label.strip():
            out.append((label.strip(), node))
    return out


# ---- S_NATIVE (native <select>) ----
async def _s_native(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_NATIVE")
    options = await act.read_options(session, ctx.node)  # in-DOM <option> texts, free
    if not options:
        ctx.trace.append("native-no-options")
        return ESCALATE if ctx.required else SKIP
    chosen = await brain.pick_option(ctx.value, options, llm=ctx.llm)
    if not chosen:
        return await _s_other_guard(session, ctx)
    ok = await act.select_option(session, ctx.node, chosen)
    if not ok:
        ctx.trace.append("native-select-refused")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = chosen
    return await _s_verify(session, ctx)


# ---- S_DATE ----
async def _s_date(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_DATE")
    # Never delta-probed. Type the value into the located control; a segmented control accepts
    # digit-by-digit via the same trusted type. (Segment-aware digit building is the engine's
    # _date job for Workday; here the located node is the date control and trusted-type lands it.)
    ok = await act.type_text(session, ctx.node, ctx.value, clear=True)
    if not ok:
        ctx.trace.append("date-type-refused")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = ctx.value
    return await _s_verify(session, ctx)


# ---- S3_OPEN (click & watch for a field-scoped delta) ----
async def _s3_open(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S3_OPEN")
    # First try the cheap inspectable read (native/ARIA/custom dropdown_options).
    inspect = await act.read_options(session, ctx.node)
    if inspect:
        ctx.trace.append(f"read_options:{len(inspect)}")
        return await _commit_from_options(session, ctx, inspect, nodes=None)
    # Else physically open and watch the delta.
    before = await perc.get_state(session)
    await act.click_node(session, ctx.node)
    cluster = await _settle(session, before, _SETTLE_STATIC_S)
    texts = _option_texts(cluster)
    if not texts:
        ctx.trace.append("no-delta->search")
        return await _s4_search(session, ctx)  # DISAMBIGUATE — never assume text
    ctx.trace.append(f"delta-cluster:{len(texts)}")
    return await _commit_from_options(session, ctx, texts, nodes=cluster)


async def _commit_from_options(session: Any, ctx: Ctx, texts: list[str], nodes: list[perc.DeltaNode] | None) -> Outcome:
    """S_CLOSED_LIST: pick + commit from a read option set (with optional cluster nodes).
    Long list -> bounded scroll-reread on no-match. Records committed_text."""
    if len(texts) >= _LIST_LONG:
        ctx.trace.append("long-list")
    chosen = await brain.pick_option(ctx.value, texts, llm=ctx.llm)
    if not chosen and nodes is not None and ctx.scroll_reads < SCROLL_CAP:
        # scroll the overlay one page and re-read (off-screen / virtualized, §3.5 / gap E)
        ctx.scroll_reads += 1
        container = nodes[0].node if nodes else None
        before = await perc.get_state(session)
        await act.scroll(session, container, _SCROLL_PX)
        more = await _settle(session, before, _SETTLE_STATIC_S)
        texts2 = _option_texts(nodes + more)
        ctx.trace.append(f"scroll-reread:{len(texts2)}")
        return await _commit_from_options(session, ctx, texts2, nodes + more)
    if not chosen:
        return await _s_other_guard(session, ctx)

    ctx.committed_text = chosen
    committed = False
    if nodes is not None:
        node = _node_for_option(nodes, chosen)
        if node is not None:
            committed = await act.click_node(session, node)  # TRUSTED click the option cell
    if not committed:
        # inspectable widget OR Enter-on-highlight commit path
        committed = await act.select_option(session, ctx.node, chosen)
    if not committed:
        ctx.trace.append("commit-failed")
        return ESCALATE if ctx.required else SKIP

    if ctx.nature == "MULTI":
        return await _s_multi_loop(session, ctx)
    return await _s_cascade(session, ctx)


# ---- S4_SEARCH (typeahead search-loop, §5) ----
async def _s4_search(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S4_SEARCH")
    # Precondition: the located element must be text-editable (a native <select> would have
    # been caught by intrinsic and routed to S_NATIVE).
    variants = await brain.query_variants(ctx.value, ctx.nature or "SEARCH", llm=ctx.llm)
    for q in variants:
        if ctx.search_tries >= VARIANT_CAP:
            break
        if q.lower() in {x.lower() for x in ctx.queries_tried}:
            continue
        ctx.queries_tried.append(q)
        ctx.search_tries += 1
        before = await perc.get_state(session)
        probe = q[: min(len(q), 4)] if len(q) > 4 else q
        typed = await act.type_text(session, ctx.node, probe, clear=True)
        if not typed:
            continue
        # type the rest so a server-side filter sees the full distinctive query
        if len(q) > len(probe):
            await act.type_text(session, ctx.node, q[len(probe) :], clear=False)
        cluster = await _settle(session, before, _SETTLE_SEARCH_S)
        texts = _option_texts(cluster)
        ctx.trace.append(f"search '{q}' -> {len(texts)} opts")
        if not texts:
            continue  # this variant produced nothing — advance
        chosen = await brain.pick_option(ctx.value, texts, llm=ctx.llm)
        if not chosen:
            continue
        ctx.committed_text = chosen
        node = _node_for_option(cluster, chosen)
        committed = False
        if node is not None:
            committed = await act.click_node(session, node)
        if not committed:
            # Enter-on-highlight commit (geocomplete: blur discards — Enter commits, §5)
            await act.press_key(session, "Enter")
            committed = True
        if committed:
            if ctx.nature == "MULTI":
                return await _s_multi_loop(session, ctx)
            return await _s_cascade(session, ctx)

    # exhausted, no overlay anywhere.
    if _is_plain_text_editable(ctx.node) and ctx.nature == "FREE_TEXT":
        ctx.trace.append("no-overlay->text(free_text_ok)")
        return await _s_text(session, ctx)
    ctx.trace.append("search-exhausted")
    return await _s_other_guard(session, ctx)


# ---- S_TEXT_GUARD / S_TEXT ----
async def _s_text_guard(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_TEXT_GUARD")
    # Defend a map mis-tag: if the element is actually a combobox, route to search, never type.
    if not _is_plain_text_editable(ctx.node):
        ctx.trace.append("not-plain-text->search")
        return await _s4_search(session, ctx)
    return await _s_text(session, ctx)


async def _s_text(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_TEXT")
    ok = await act.type_text(session, ctx.node, ctx.value, clear=True)
    if not ok:
        ctx.trace.append("text-type-refused")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = ctx.value
    return await _s_verify(session, ctx)


# ---- S_CASCADE (did the commit reveal a sub-field?) ----
async def _s_cascade(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_CASCADE")
    # Bounded; the single-page proof does not derive child values (no profile here), so cascade
    # only guards against runaway recursion and hands off to verify. A real sub-field would be
    # discovered by the runner's next field; here we verify the parent commit.
    return await _s_verify(session, ctx)


# ---- S_MULTI_LOOP (multi-value chips: Skills / Languages) ----
async def _s_multi_loop(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_MULTI_LOOP")
    parts = [p.strip() for p in ctx.value.replace(";", ",").split(",") if p.strip()]
    ctx.multi_done += 1  # the first value was committed by the caller
    for part in parts[ctx.multi_done :]:
        if ctx.multi_done >= MULTI_CAP:
            break
        if not ctx.guard():
            break
        before = await perc.get_state(session)
        typed = await act.type_text(session, ctx.node, part, clear=True)
        if not typed:
            continue
        cluster = await _settle(session, before, _SETTLE_SEARCH_S)
        texts = _option_texts(cluster)
        chosen = await brain.pick_option(part, texts, llm=ctx.llm) if texts else None
        node = _node_for_option(cluster, chosen) if chosen else None
        if node is not None:
            await act.click_node(session, node)
        elif chosen:
            await act.press_key(session, "Enter")
        ctx.multi_done += 1
        ctx.trace.append(f"multi+ {part}")
    return await _s_verify(session, ctx)


# ---- verify oracle wrapper: DOM read-back FIRST, VLM as a per-FIELD-budgeted AID (§6.1) ----
async def _verify_field(session: Any, ctx: Ctx, *, key: str) -> str:
    """Run ONE value-aware verify of the located control via the DOM-first oracle.

    Enforces the per-FIELD budget (NOT per-page, so a huge single page never starves field 7+):
      * ``FIELD_VERIFY_CAP`` total verify attempts (DOM read-back + VLM aids combined),
      * ``FIELD_VLM_CAP`` VLM aids — DOM read-back is free and primary, the VLM is only an aid
        when the DOM read is empty/ambiguous, so ``allow_vlm`` is False once the field's VLM
        sub-budget is spent (then ``brain.verify`` returns EMPTY/UNKNOWN without spending).

    The per-PAGE ``vision_verify.VLM_MAX_CALLS`` gate still exists as a backstop, but the page is
    set to a HIGH backstop at run start (see ``reset_page_vlm_backstop``) so it cannot pre-empt the
    per-field budget on a long single page (the field-7+ starvation root cause)."""
    if ctx.verify_used >= FIELD_VERIFY_CAP:
        ctx.trace.append("verify-cap")
        return "UNKNOWN"
    ctx.verify_used += 1
    allow_vlm = ctx.vlm_used < FIELD_VLM_CAP
    # snapshot the page VLM counter to detect whether the AID was actually spent.
    before_n = _vv_calls()
    verdict = await brain.verify(
        session, ctx.label, ctx.value, node=ctx.node, llm=ctx.llm, key=key, use_cache=True, allow_vlm=allow_vlm
    )
    spent = max(0, _vv_calls() - before_n)
    if spent:
        ctx.vlm_used += spent
    # Record the verdict SOURCE so the proof can audit dom-vs-vlm per field (no per-ATS branching;
    # purely derived from whether the VLM AID was actually consulted on this verify call).
    ctx.trace.append(f"verify-src:{'vlm' if spent else 'dom'}")
    return verdict


def _vv_calls() -> int:
    """Current per-page VLM call count (vision_verify's module counter), for per-field accounting."""
    import vision_verify as _vv

    return int(_vv._VLM_CALLS.get("n", 0))


def reset_page_vlm_backstop(high: int = 10_000) -> None:
    """Lift the per-PAGE VLM cap to a high backstop so the per-FIELD budget (FIELD_VLM_CAP) is the
    real limiter on a long single page. The runner calls this once per page/record; the per-field
    budget then prevents any single field from over-spending the VLM. (Keeps the cache, which is
    correct + free.)"""
    import vision_verify as _vv

    _vv.VLM_MAX_CALLS = int(high)


# ---- S_VERIFY (DOM read-back primary + VLM aid + 3-way routing, §6) ----
async def _s_verify(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_VERIFY")
    # §6.1 fast-path: a deterministic DOM-identity commit (native select / file) is mechanically
    # reliable — return DONE without any read-back, as today.
    if ctx.nature in ("INTRINSIC_SELECT", "INTRINSIC_FILE") and ctx.committed_text and not ctx.ambiguous:
        ctx.trace.append("fast-path-DONE")
        return DONE

    verdict = await _verify_field(session, ctx, key=ctx.label)
    ctx.trace.append(f"verdict:{verdict}")
    if verdict == "CORRECT":
        return DONE
    if verdict == "EMPTY":
        return await _s_recommit(session, ctx)
    if verdict == "WRONG":
        return await _s_revalue(session, ctx)
    # UNKNOWN routing (§6.3): required SEARCH/lagged -> ESCALATE (never DONE); optional -> SKIP;
    # intrinsic/native reliable commit -> accept on the free CDP/DOM truth.
    if ctx.nature in ("INTRINSIC_FILE", "INTRINSIC_SELECT", "INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
        return DONE
    return ESCALATE if ctx.required else SKIP


# ---- S_RECOMMIT (EMPTY: click never registered) ----
async def _s_recommit(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    if ctx.commit_tries >= COMMIT_CAP:
        ctx.trace.append("commit-cap")
        return ESCALATE if ctx.required else SKIP
    ctx.commit_tries += 1
    ctx.trace.append(f"S_RECOMMIT#{ctx.commit_tries}")
    # Re-issue the SAME commit on the resolved element — NEVER a new query.
    if ctx.nature == "INTRINSIC_SELECT":
        await act.select_option(session, ctx.node, ctx.committed_text or ctx.value)
    elif _is_plain_text_editable(ctx.node) or ctx.nature in ("FREE_TEXT", "DATE"):
        await act.type_text(session, ctx.node, ctx.value, clear=True)
    else:
        await act.click_node(session, ctx.node)
    # fresh verify key so the re-read is not served the cached stale EMPTY (§6.2); DOM-first +
    # per-field budget enforced via _verify_field.
    verdict = await _verify_field(session, ctx, key=f"{ctx.label}:commit#{ctx.commit_tries}")
    ctx.trace.append(f"recommit-verdict:{verdict}")
    if verdict == "CORRECT":
        return DONE
    if verdict == "EMPTY":
        return await _s_recommit(session, ctx)
    if verdict == "WRONG":
        return await _s_revalue(session, ctx)
    return DONE if ctx.nature.startswith("INTRINSIC") else (ESCALATE if ctx.required else SKIP)


# ---- S_REVALUE (WRONG: committed a different, non-blank option) ----
async def _s_revalue(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    if ctx.revalue_tries >= REVALUE_CAP:
        ctx.trace.append("revalue-cap")
        return ESCALATE if ctx.required else SKIP
    ctx.revalue_tries += 1
    ctx.trace.append(f"S_REVALUE#{ctx.revalue_tries}")
    # clear the wrong value, then re-search with the next UNUSED variant (dedup via queries_tried).
    await act.type_text(session, ctx.node, "", clear=True)
    return await _s4_search(session, ctx)


# ---- S_OTHER_GUARD / S_OTHER ----
async def _s_other_guard(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_OTHER_GUARD")
    if not ctx.required:
        return SKIP
    # NEVER a silent Other on demographic / screening / legal labels.
    if _is_sensitive(ctx.label):
        ctx.trace.append("sensitive->ESCALATE")
        return ESCALATE
    return await _s_other(session, ctx)


async def _s_other(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_OTHER")
    # An Other/Prefer-not escape must be a GENUINE rendered option (no fabrication).
    options = await act.read_options(session, ctx.node)
    if options:
        for esc in ("Other", "Prefer not to say", "Prefer not to answer", "N/A", "Decline to self identify"):
            match = next((o for o in options if o.strip().lower() == esc.lower()), None)
            if match:
                ok = await act.select_option(session, ctx.node, match)
                if ok:
                    ctx.committed_text = match
                    ctx.trace.append(f"other->{match}")
                    return OTHER
    ctx.trace.append("no-escape->ESCALATE")
    return ESCALATE


# --------------------------------------------------------------------------- #
# OFFLINE self-test — drives the WHOLE machine over a FAKE session that serves
# scripted browser-use states + records actions. No browser, no network, no $.
# Proves: classify routing (intrinsic + label-meaning), the delta-open closed-list
# path, the search-loop, free-text guard, verify 3-way routing, sensitive Other-guard.
# --------------------------------------------------------------------------- #
def _fake_node(tag="div", typ=None, role=None, ax_name=None, attrs=None, box=(10, 10, 200, 30), visible=True):
    from browser_use.dom.views import DOMRect, EnhancedAXNode, EnhancedDOMTreeNode, NodeType

    a = dict(attrs or {})
    if typ:
        a["type"] = typ
    ax = None
    if role or ax_name:
        ax = EnhancedAXNode(
            ax_node_id="ax",
            ignored=False,
            role=role,
            name=ax_name,
            description=None,
            properties=None,
            child_ids=None,
        )
    rect = DOMRect(x=box[0], y=box[1], width=box[2], height=box[3]) if box else None
    return EnhancedDOMTreeNode(
        node_id=_fake_node._n,
        backend_node_id=_fake_node._n,
        node_type=NodeType.ELEMENT_NODE,
        node_name=tag.upper(),
        node_value="",
        attributes=a,
        is_scrollable=False,
        is_visible=visible,
        absolute_position=rect,
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[],
        ax_node=ax,
        snapshot_node=None,
    )


_fake_node._n = 0  # type: ignore[attr-defined]


def _mk(**kw):
    _fake_node._n += 1  # type: ignore[attr-defined]
    return _fake_node(**kw)


async def _selftest() -> int:
    checks: list[tuple[str, bool, Any]] = []

    def chk(name, passed, detail=""):
        checks.append((name, passed, detail))

    # --- pure intrinsic classification (no session needed) ---
    chk("intrinsic file", classify_intrinsic(_mk(tag="input", typ="file")) == "INTRINSIC_FILE")
    chk("intrinsic radio", classify_intrinsic(_mk(tag="input", typ="radio")) == "INTRINSIC_RADIO")
    chk("intrinsic checkbox(role)", classify_intrinsic(_mk(role="checkbox")) == "INTRINSIC_CHECKBOX")
    chk("intrinsic select", classify_intrinsic(_mk(tag="select")) == "INTRINSIC_SELECT")
    chk("intrinsic date(spinbutton)", classify_intrinsic(_mk(role="spinbutton")) == "INTRINSIC_DATE")
    chk("non-intrinsic ''", classify_intrinsic(_mk(tag="input", typ="text", role="combobox")) == "")
    chk(
        "plain-text editable yes",
        _is_plain_text_editable(_mk(tag="textarea")) and _is_plain_text_editable(_mk(tag="input", typ="text")),
    )
    chk(
        "combobox is NOT plain text",
        not _is_plain_text_editable(_mk(tag="input", typ="text", role="combobox")),
    )
    chk("sensitive label guarded", _is_sensitive("Are you authorized to work?") and _is_sensitive("Gender"))
    chk("non-sensitive label free", not _is_sensitive("Degree"))

    # --- whole-machine over a scripted FAKE session ---
    from oa_observe_act_fakes import FakeSession, GenericFakeLLM, make_card  # helpers alongside the tests

    fake_llm = GenericFakeLLM()

    # (1) closed list: click opens a 3-option menu, pick Bachelor's, VLM says CORRECT.
    degree_node = _mk(tag="input", role="combobox", ax_name="Degree")
    fs = FakeSession(
        controls=[degree_node],
        on_click_delta={
            degree_node.backend_node_id: [
                ("Bachelor's Degree", (100, 250)),
                ("Master's Degree", (100, 280)),
                ("Doctorate", (100, 310)),
            ]
        },
        read_options_map={},  # force the click+delta path
        verdict='{"filled": true, "value": "Bachelor\'s Degree", "matches": true}',
    )
    out = await observe_act(fs, {"label": "Degree", "value": "Bachelor's Degree", "required": True, "llm": fake_llm})
    chk("closed-list DONE", out == DONE, out)
    chk("closed-list committed text", fs.last_click_text == "Bachelor's Degree", fs.last_click_text)

    # (2) native select: read_options returns texts, select_option commits, fast-path DONE.
    sel_node = _mk(tag="select", ax_name="State")
    fs2 = FakeSession(
        controls=[sel_node],
        read_options_map={sel_node.backend_node_id: ["California", "New York", "Texas"]},
        verdict='{"filled": true, "matches": true}',
    )
    out2 = await observe_act(fs2, {"label": "State", "value": "California", "required": True, "llm": fake_llm})
    chk("native-select DONE", out2 == DONE, out2)
    chk("native-select selected California", fs2.last_select_text == "California", fs2.last_select_text)

    # (3) free-text: a plain textarea, FREE_TEXT nature -> type, VLM CORRECT.
    ta = _mk(tag="textarea", ax_name="Why do you want to work here?")
    fs3 = FakeSession(controls=[ta], read_options_map={}, verdict='{"filled": true, "matches": true}')
    out3 = await observe_act(
        fs3,
        {"label": "Why do you want to work here?", "value": "Because I love it.", "required": True, "llm": fake_llm},
    )
    chk("free-text DONE", out3 == DONE, out3)
    chk("free-text typed value", fs3.last_type_text == "Because I love it.", fs3.last_type_text)

    # (4) search loop: combobox 'School', typing 'UCLA' yields a 1-option delta, Enter commits.
    school = _mk(tag="input", role="combobox", attrs={"aria-autocomplete": "list"}, ax_name="School")
    fs4 = FakeSession(
        controls=[school],
        on_type_delta={school.backend_node_id: [("University of California, Los Angeles", (100, 250))]},
        read_options_map={},
        verdict='{"filled": true, "matches": true}',
    )
    out4 = await observe_act(fs4, {"label": "School", "value": "UCLA", "required": True, "llm": fake_llm})
    chk("search-loop DONE", out4 == DONE, out4)

    # (5) blank optional -> SKIP, no work.
    out5 = await observe_act(FakeSession(controls=[ta]), {"label": "Salary", "value": "", "required": False})
    chk("blank -> SKIP", out5 == SKIP, out5)

    # (6) verify EMPTY -> recommit -> CORRECT.
    sel6 = _mk(tag="select", ax_name="Country")
    fs6 = FakeSession(
        controls=[sel6],
        read_options_map={sel6.backend_node_id: ["United States", "Canada"]},
        verdict_sequence=[
            '{"filled": false, "value": ""}',  # first verify: EMPTY
            '{"filled": true, "matches": true}',  # recommit verify: CORRECT
        ],
    )
    out6 = await observe_act(fs6, {"label": "Country", "value": "United States", "required": True, "llm": fake_llm})
    # native-select fast-path returns DONE before the EMPTY can route; assert DONE either way.
    chk("verify/native DONE", out6 == DONE, out6)

    # (7) sensitive required no-match -> ESCALATE, never silent Other.
    race = _mk(tag="input", role="combobox", ax_name="Race / Ethnicity")
    fs7 = FakeSession(
        controls=[race],
        on_click_delta={race.backend_node_id: []},  # nothing opens
        on_type_delta={race.backend_node_id: []},  # search finds nothing
        read_options_map={},
        verdict='{"filled": false}',
    )
    out7 = await observe_act(fs7, {"label": "Race / Ethnicity", "value": "Martian", "required": True, "llm": fake_llm})
    chk("sensitive no-match -> ESCALATE", out7 == ESCALATE, out7)

    # (8) global step cap terminates (force a tiny cap).
    ctx = Ctx(label="Loop", value="x", required=False)
    ctx.steps = STEP_CAP  # next guard trips
    chk("guard trips at cap", ctx.guard() is False)

    # (9) DOM-FIRST verify ORACLE: a plain text control whose live DOM value == want verifies
    #     CORRECT with ZERO VLM calls (the whole point — free DOM read-back is the truth).
    import vision_verify as _vv

    _vv._VLM_CALLS["n"] = 0
    reset_page_vlm_backstop()
    name_node = _mk(tag="input", typ="text", ax_name="First Name")
    fs9 = FakeSession(
        controls=[name_node],
        dom_values={name_node.backend_node_id: "Pyry"},  # the control already holds the value
        verdict='{"filled": true, "matches": true}',  # would say CORRECT too, but must NOT be reached
    )
    out9 = await observe_act(fs9, {"label": "First Name", "value": "Pyry", "required": True, "llm": fake_llm})
    chk("DOM-first text DONE (no VLM)", out9 == DONE and fs9.vlm_calls == 0, (out9, fs9.vlm_calls))

    # (10) VISUAL-ONLY widget: DOM read empty -> the engine consults the VLM AID (>=1 call).
    _vv._VLM_CALLS["n"] = 0
    widget = _mk(tag="textarea", ax_name="Why do you want to work here?")
    fs10 = FakeSession(
        controls=[widget],
        dom_values={},  # read_dom_value -> "" (visual-only) -> VLM aid path
        verdict='{"filled": true, "matches": true}',
    )
    out10 = await observe_act(
        fs10,
        {"label": "Why do you want to work here?", "value": "Because.", "required": True, "llm": fake_llm},
    )
    chk("visual-only consults VLM aid", out10 == DONE and fs10.vlm_calls >= 1, (out10, fs10.vlm_calls))

    # (11) per-FIELD VLM budget caps at <=FIELD_VLM_CAP: a visual-only field that keeps reading EMPTY
    #      (commit never registers) must stop spending the VLM at FIELD_VLM_CAP aids, never per-page.
    _vv._VLM_CALLS["n"] = 0
    stubborn = _mk(tag="textarea", ax_name="Stuck Field")
    fs11 = FakeSession(
        controls=[stubborn],
        dom_values={},  # always visual-only -> every verify is a VLM aid
        verdict='{"filled": false, "value": ""}',  # always EMPTY -> recommit loop drives more verifies
    )
    out11 = await observe_act(fs11, {"label": "Stuck Field", "value": "x", "required": False, "llm": fake_llm})
    chk(
        "per-FIELD VLM budget caps at <=FIELD_VLM_CAP",
        fs11.vlm_calls <= FIELD_VLM_CAP,
        (out11, fs11.vlm_calls, FIELD_VLM_CAP),
    )

    # (12) FIX 1 — LEVER-CARD SPATIAL LOCATE: an input whose visible question is NOT wired to it
    #      (blank accessible name) is bound by Tier-2 spatial proximity (the wrapper's question text),
    #      then typed + DOM-verified. Tier-1 structure alone would have returned no-control.
    _vv._VLM_CALLS["n"] = 0
    card_input = make_card("Why do you want to work here?", input_bnid=4242, role="textbox")
    fs12 = FakeSession(
        controls=[card_input],
        dom_values={card_input.backend_node_id: "Because I love the mission."},  # DOM read-back truth
        verdict='{"filled": true, "matches": true}',
    )
    fd12 = {
        "label": "Why do you want to work here?",
        "value": "Because I love the mission.",
        "required": True,
        "llm": fake_llm,
    }
    out12 = await observe_act(fs12, fd12)
    located_spatial = "located:spatial" in (fd12.get("_trace") or [])
    chk("Lever-card bound by SPATIAL locate -> DONE", out12 == DONE and located_spatial, (out12, fd12.get("_trace")))

    # (13) FIX 2 — GLOBAL FILE PATH finds a HIDDEN input[type=file] (no readable label) and uploads it.
    from oa_observe_act_fakes import _hidden_file_input  # builder for a zero-box file input

    hidden_file = _hidden_file_input(5252)
    fs13 = FakeSession(controls=[hidden_file])
    out13 = await observe_act(
        fs13,
        {"label": "Resume/CV", "value": "", "required": True, "resume": "/fixtures/test_resume.pdf", "llm": fake_llm},
    )
    chk(
        "hidden file input found + uploaded -> DONE",
        out13 == DONE and fs13.last_upload == "/fixtures/test_resume.pdf",
        (out13, fs13.last_upload),
    )

    # (14) FIX 2 — ALREADY-UPLOADED is idempotent: a file already attached returns DONE, NO re-upload.
    hidden_file2 = _hidden_file_input(5353)
    fs14 = FakeSession(
        controls=[hidden_file2],
        dom_values={hidden_file2.backend_node_id: "FILE:resume.pdf"},  # is_already_uploaded -> True
    )
    out14 = await observe_act(
        fs14,
        {"label": "Resume", "value": "", "required": True, "resume": "/fixtures/test_resume.pdf", "llm": fake_llm},
    )
    chk(
        "already-uploaded -> DONE, NO re-upload",
        out14 == DONE and fs14.last_upload is None,
        (out14, fs14.last_upload),
    )

    # (15) FIX 3 — a normal text field costs FEW full-page get_state serializes (<=3). The locate does
    #      ONE; verify uses the cheap CDP single-node read (NOT get_state). No settle on a plain field.
    name15 = _mk(tag="input", typ="text", ax_name="Last Name")
    fs15 = FakeSession(controls=[name15], dom_values={name15.backend_node_id: "Halonen"})
    out15 = await observe_act(fs15, {"label": "Last Name", "value": "Halonen", "required": True, "llm": fake_llm})
    chk(
        "FIX 3: normal field uses <=3 get_state",
        out15 == DONE and fs15.state_reads <= 3,
        (out15, fs15.state_reads),
    )

    # ---- THE GAP FIX: grouped-widget locate binds a question HEADING to its non-text card control(s).
    from oa_observe_act_fakes import (  # card builders + below-the-fold session
        _ScrollRevealSession,
        make_choice_card,
        make_single_input_card,
    )

    # (16) RADIO CARD: heading "Are you authorized to work?" + two radios (Yes/No) NOT wired to the
    #      heading. Tier-1/shallow-spatial miss (the radios' own names are 'Yes'/'No'); the grouped
    #      tier binds the card -> INTRINSIC_RADIO -> _s_choice picks 'Yes' -> DOM read-back CORRECT.
    yes_no = make_choice_card("Are you authorized to work?", ["Yes", "No"], base_bnid=600, kind="radio")
    fs16 = FakeSession(
        controls=yes_no,
        dom_values={yes_no[0].backend_node_id: "Yes"},  # the 'Yes' radio reads back checked
    )
    fd16 = {"label": "Are you authorized to work?", "value": "Yes", "required": True, "llm": fake_llm}
    out16 = await observe_act(fs16, fd16)
    tr16 = fd16.get("_trace") or []
    chk(
        "RADIO card bound by GROUPED locate -> _s_choice 'Yes' -> DONE",
        out16 == DONE and "located:grouped" in tr16 and fd16.get("_committed") == "Yes",
        (out16, fd16.get("_committed"), tr16),
    )

    # (17) CHECKBOX CARD: heading "Language Skill(s) (Check all that apply)" + 3 checkboxes; value
    #      'Spanish' -> grouped bind -> INTRINSIC_CHECKBOX -> _s_choice picks 'Spanish' (NOT English/
    #      French) -> DOM CORRECT. Proves the choice group is SCOPED to this card's options.
    langs = make_choice_card(
        "Language Skill(s) (Check all that apply)", ["English", "Spanish", "French"], base_bnid=620, kind="checkbox"
    )
    spanish = langs[1]
    fs17 = FakeSession(controls=langs, dom_values={spanish.backend_node_id: "Spanish"})
    fd17 = {
        "label": "Language Skill(s) (Check all that apply)",
        "value": "Spanish",
        "required": True,
        "llm": fake_llm,
    }
    out17 = await observe_act(fs17, fd17)
    tr17 = fd17.get("_trace") or []
    chk(
        "CHECKBOX card bound + scoped -> _s_choice 'Spanish' -> DONE",
        out17 == DONE and "located:grouped" in tr17 and fd17.get("_committed") == "Spanish",
        (out17, fd17.get("_committed"), tr17),
    )

    # (18) SINGLE_SELECT CARD: heading "How did you hear about us?" + a native <select> NOT wired to
    #      the heading -> grouped bind -> INTRINSIC_SELECT -> _s_native picks 'LinkedIn' -> fast-path DONE.
    hear = make_single_input_card("How did you hear about us?", bnid=640, tag="select")
    fs18 = FakeSession(
        controls=[hear],
        read_options_map={hear.backend_node_id: ["LinkedIn", "Referral", "Job board"]},
        verdict='{"filled": true, "matches": true}',
    )
    fd18 = {"label": "How did you hear about us?", "value": "LinkedIn", "required": True, "llm": fake_llm}
    out18 = await observe_act(fs18, fd18)
    tr18 = fd18.get("_trace") or []
    chk(
        "SINGLE_SELECT card bound by GROUPED locate -> _s_native -> DONE",
        out18 == DONE and "located:grouped" in tr18 and fs18.last_select_text == "LinkedIn",
        (out18, fs18.last_select_text, tr18),
    )

    # (19) TEXTAREA CARD: heading "Why do you want to work at Palantir?" + a textarea NOT wired to it
    #      -> grouped bind -> FREE_TEXT -> _s_text types the value -> DOM read-back CORRECT.
    why = make_single_input_card("Why do you want to work at Palantir?", bnid=660, tag="textarea")
    fs19 = FakeSession(
        controls=[why],
        dom_values={why.backend_node_id: "Because I admire the mission."},
        verdict='{"filled": true, "matches": true}',
    )
    fd19 = {
        "label": "Why do you want to work at Palantir?",
        "value": "Because I admire the mission.",
        "required": True,
        "llm": fake_llm,
    }
    out19 = await observe_act(fs19, fd19)
    tr19 = fd19.get("_trace") or []
    chk(
        "TEXTAREA card bound by GROUPED locate -> _s_text -> DONE",
        out19 == DONE and "located:grouped" in tr19 and fs19.last_type_text == "Because I admire the mission.",
        (out19, fs19.last_type_text, tr19),
    )

    # (20) CHOICE-GROUP SCOPING: two radio cards on ONE page (authorize Yes/No + sponsorship Yes/No).
    #      Filling the sponsorship question must pick from ITS card, not the authorize card — proves
    #      _read_choice_group is scoped to the bound card, not grouped by intrinsic-kind page-wide.
    auth = make_choice_card("Are you legally authorized to work?", ["Yes", "No"], base_bnid=700, kind="radio", top=200)
    spon = make_choice_card("Will you require visa sponsorship?", ["Yes", "No"], base_bnid=720, kind="radio", top=400)
    fs20 = FakeSession(
        controls=auth + spon,
        dom_values={spon[1].backend_node_id: "No"},  # the sponsorship 'No' radio reads back checked
    )
    fd20 = {"label": "Will you require visa sponsorship?", "value": "No", "required": True, "llm": fake_llm}
    out20 = await observe_act(fs20, fd20)
    tr20 = fd20.get("_trace") or []
    # The committed node must be the SPONSORSHIP card's 'No' (bnid in spon), never the authorize card's.
    chk(
        "CHOICE GROUP scoped to the RIGHT card (sponsorship 'No')",
        out20 == DONE and "located:grouped" in tr20 and fd20.get("_committed") == "No",
        (out20, fd20.get("_committed"), tr20),
    )

    # (21) BELOW-THE-FOLD: a textarea card initially not-visible becomes visible after a bounded scroll;
    #      the scroll-locate re-locate binds it. Proves a question is not missed just for being lower.
    below = make_single_input_card("Tell us about a project you are proud of?", bnid=680, tag="textarea", visible=False)
    fs21 = _ScrollRevealSession(
        controls=[below],
        reveal_bnid=below.backend_node_id,
        dom_values={below.backend_node_id: "I built a compiler."},
    )
    fd21 = {
        "label": "Tell us about a project you are proud of?",
        "value": "I built a compiler.",
        "required": True,
        "llm": fake_llm,
    }
    os.environ["OA_SCROLL_LOCATE"] = "1"  # scroll-locate is opt-in (heavy on live SPAs); enable it for this path's test
    try:
        out21 = await observe_act(fs21, fd21)
    finally:
        os.environ.pop("OA_SCROLL_LOCATE", None)
    tr21 = fd21.get("_trace") or []
    chk(
        "BELOW-THE-FOLD card found after bounded scroll (opt-in) -> DONE",
        out21 == DONE and any(t.startswith("scroll-locate#") and t.endswith("hit") for t in tr21),
        (out21, tr21),
    )

    # (22) BUILD FIX C — VISUAL SET-OF-MARKS bind of a LABEL-FREE card. A radio group whose HEADING
    #      ("Work eligibility") shares NO tokens with the question ("Are you authorized to work?") —
    #      structure (radios named Yes/No), shallow spatial, AND grouped-text ALL miss. The marks tier
    #      screenshots + marks the candidate radios and the (fake) VLM returns the 'Yes' radio's bnid;
    #      it routes to _s_choice (intrinsic radio) and DOM-verifies CORRECT. Proves the visual bridge
    #      binds a card with no structural label, the way a human SEES it.
    from oa_observe_act_fakes import install_marks_vlm, make_labelfree_choice_card, restore_vlm

    _vv._VLM_CALLS["n"] = 0
    reset_page_vlm_backstop()
    lf = make_labelfree_choice_card("Work eligibility", ["Yes", "No"], base_bnid=900, kind="radio")
    yes_bnid = lf[0].backend_node_id
    fs22 = FakeSession(controls=lf, dom_values={yes_bnid: "Yes"})
    orig_vlm = install_marks_vlm(f'{{"mark": {yes_bnid}}}')
    try:
        fd22 = {"label": "Are you authorized to work?", "value": "Yes", "required": True, "llm": fake_llm}
        out22 = await observe_act(fs22, fd22)
    finally:
        restore_vlm(orig_vlm)
    tr22 = fd22.get("_trace") or []
    chk(
        "LABEL-FREE card bound by VISUAL set-of-marks -> _s_choice 'Yes' -> DONE",
        out22 == DONE and "located:marks" in tr22 and fd22.get("_committed") == "Yes",
        (out22, fd22.get("_committed"), tr22),
    )
    chk("marks tier spent the VLM aid (set-of-marks)", "locate-marks-aid" in tr22, tr22)

    # (23) BUILD FIX C — LOCATION classifies as SEARCH, never MULTI. A single 'Current location' value
    #      that carries a comma ("San Francisco, CA") must NOT be split across pills. Drive a combobox
    #      location field with a comma value and assert the nature is SEARCH (the search-loop), the
    #      typeahead path — not MULTI / S_MULTI_LOOP.
    loc = _mk(tag="input", role="combobox", attrs={"aria-autocomplete": "list"}, ax_name="Current location")
    fs23 = FakeSession(
        controls=[loc],
        on_type_delta={loc.backend_node_id: [("San Francisco, CA, United States", (100, 250))]},
        dom_values={loc.backend_node_id: "San Francisco, CA, United States"},
        verdict='{"filled": true, "matches": true}',
    )
    fd23 = {"label": "Current location", "value": "San Francisco, CA", "required": True, "llm": fake_llm}
    out23 = await observe_act(fs23, fd23)
    tr23 = fd23.get("_trace") or []
    chk(
        "LOCATION w/ comma -> SEARCH (not MULTI), search-loop fires",
        fd23.get("_nature") == "SEARCH" and "S4_SEARCH" in tr23 and "S_MULTI_LOOP" not in tr23,
        (out23, fd23.get("_nature"), tr23),
    )

    # (24) BUILD FIX C — a GENUINE multi field (Skills) with a comma value STILL classifies MULTI.
    #      Proves the classify tightening did not regress real multi-value fields.
    skills = _mk(tag="input", role="combobox", attrs={"aria-autocomplete": "list"}, ax_name="Skills")
    fs24 = FakeSession(
        controls=[skills],
        on_type_delta={skills.backend_node_id: [("Python", (100, 250))]},
        dom_values={skills.backend_node_id: "Python"},
    )
    fd24 = {"label": "Skills", "value": "Python, Go", "required": False, "cardinality": "many", "llm": fake_llm}
    out24 = await observe_act(fs24, fd24)
    chk("genuine multi (Skills) still MULTI", fd24.get("_nature") == "MULTI", (out24, fd24.get("_nature")))

    ok = True
    print("\n=== oa_observe_act offline self-test (fake session+llm, no browser/VLM, $0) ===")
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(_selftest()))
