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
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import oa_action as act
import oa_brain as brain
import oa_cdp_action as cdpa
import oa_file_locate as filoc
import oa_perception as perc

# --------------------------------------------------------------------------- #
# Outcome terminals (§2). Mapped to the ladder in §6.5 by the runner.
# --------------------------------------------------------------------------- #
DONE = "DONE"  # field visibly holds the value (or a clearly-equivalent option)
OTHER = "OTHER"  # required, no exact match -> a genuine "Other"/"Prefer not" escape committed
SKIP = "SKIP"  # optional & blank / optional & unmatchable -> left blank (agent-repairable)
ESCALATE = "ESCALATE"  # required & unfillable deterministically -> caller's agent of last resort
UNVERIFIED = "UNVERIFIED"  # committed but the vision layer could not confirm it (vision-dead) — never green
NEEDS_HUMAN = "NEEDS_HUMAN"  # a human must answer (voice/audio/media answer field) — never text-filled
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
SCROLL_CAP = 5  # off-screen / virtualized overlay reread (long EEO forms sit many viewports down)
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
# Generic catch-all wants the mapper emits when blind to a combobox's options at map-time. Not a
# per-ATS label list — this is the closed universal set of residual OPTION labels a form offers;
# a want in this set means 'the residual/none-of-the-standard-cases option', which pick_option
# resolves against the real read list (see _chosen_plausible catch-all bypass).
_CATCH_ALL_WANTS = frozenset({
    "other", "none", "none of the above", "none of these", "not applicable", "na", "n a",
    "no preference", "prefer not to answer", "prefer not to say", "decline to answer",
    "i prefer not to answer", "i decline to answer",
})
# Proven-path delegation bounds: the adapter's fill()/read_back() are CDP round-trips on a
# possibly-busy SPA. Cap each so a wedged proven commit can't eat the whole FIELD_DEADLINE —
# on timeout we fall through to the generic engine, never hang.
ADAPTER_COMMIT_TIMEOUT = float(os.environ.get("OA_ADAPTER_COMMIT_TIMEOUT", "12.0"))
ADAPTER_VERIFY_TIMEOUT = float(os.environ.get("OA_ADAPTER_VERIFY_TIMEOUT", "6.0"))

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
_SETTLE_GEO_S = 1.6  # a geocomplete (react-select location) resolves suggestions over the network
_LIST_LONG = 12  # cluster >= this -> type-to-filter first (S_CLOSED_LIST long path)
_SCROLL_PX = 720  # ~one viewport for the off-screen reread (320 barely moved on tall forms)

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


# Phantom double-bind disambiguation. A MUI Select is discovered as TWO entries (the real combobox +
# a native-mirror twin whose accessible name is the placeholder, e.g. "Select an answer"). The twin
# must be skipped so its stray value can't overwrite the real commit. But grouped-locate desync can
# also make a DISTINCT field resolve to an unrelated committed node (live Ashby: a 'visa' radio group
# resolves to the Name input's id) — that must NOT be skipped. The committer's node id is stored WITH
# its question label; a later same-node hit is a real twin only if its label is a placeholder OR shares
# a content token with the committer (principle + the DOM same-node cross-check, not a rule list).
def _phantom_label(label: str) -> str:
    return " ".join(str(label or "").lower().split())


def _phantom_twin(cur_label: str, committer_label: str) -> bool:
    # TIGHT: the twin is skippable ONLY when its OWN accessible name is a MUI Select placeholder
    # ("Select an answer/option/…", "Choose …") — the native-mirror artifact of fixtures 09/104/93.
    # A real question (a distinct radio group that grouped-desyncs onto a committed node — live Ashby
    # 'visa'/'ethnicity'/'how did you hear') never carries a placeholder name, so it is never skipped.
    # committer_label is retained for context/trace but is NOT part of the decision (a shared-token gate
    # over-fired on live neighbours). Principle + the DOM same-node cross-check, not a rule list.
    c = _phantom_label(cur_label)
    return c.startswith("select ") or c.startswith("choose ") or c in ("select", "select an answer", "select option")


# --------------------------------------------------------------------------- #
# Single-field context — the anti-spin budget the drafts lacked (§2 Ctx).
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    label: str
    value: str
    required: bool
    cardinality: str = "one"
    # KIND HINT (the card-commit fix): the adapter's OWN parsed control type — radio | checkbox |
    # single_select | select | dropdown | textarea | date | text | input_file | … — a reliable
    # STRUCTURAL fact (the adapter read the real <input type>/<select> off the live form), NOT a
    # renameable label. Honoured by ``_s2_classify`` to route a choice card to S_CHOICE / a custom
    # select to the open+read path BEFORE the label-meaning LLM guess. "" -> no hint (today's path).
    kind: str = ""
    resume: str | None = None
    llm: Any = None

    # PROVEN-PATH DELEGATION (the abstraction-layer reuse): when the run has a per-archetype
    # adapter (Greenhouse/Lever/Ashby/Workday), the COMMIT is the adapter's battle-tested
    # ``fill()`` (_fill_date / _location / _select_native / _combobox / _click_option) and the
    # VERIFY is its ``read_back()`` (lenient _is_open_ended for date/textarea). The generic engine
    # below is the FALLBACK — used only when there is no adapter (an unseen ATS) or the proven
    # commit fails its own read-back. ``field_obj`` is the adapter's original FormField (carries
    # name/selector/source); ``page`` is the BrowserSession page handle the adapter needs.
    adapter: Any = None
    page: Any = None
    field_obj: Any = None
    # run-scoped set of DOM ids already committed by an EARLIER field this page (PHANTOM double-bind
    # guard): a widget discovered as TWO fields (MUI Select native-mirror; a placeholder-labelled
    # phantom of the same control) — the second re-binds the SAME node and its commit overwrites the
    # first's. When set, S1_LOCATE skips a field that locates an already-committed node. loc_id = the
    # id this field bound, recorded into the set on DONE.
    committed_nodes: Any = None   # dict {dom_id: committer_how} — how tags whether the id is reliable
    loc_id: str = ""
    loc_how: str = ""             # this field's locate tier ('dom-ref'|'structure'|'grouped'|...)

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
    filter_tried: bool = False
    typeahead_checked: bool = False  # the blur-safety (commit-or-discard) test fired once this field
    multi_done: int = 0

    # per-FIELD verify budget (§6.1 oracle fix): DOM read-back is primary + free; the VLM is a
    # budgeted AID. verify_used counts EVERY verify call (DOM+VLM); vlm_used counts only VLM aids.
    verify_used: int = 0
    vlm_used: int = 0
    observe_used: bool = False  # observe-before-repair look fired once this field (bound, uncounted)

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


# --------------------------------------------------------------------------- #
# KIND-HINT routing (the card-commit fix). The adapter parsed each control's REAL type off the live
# form (e.g. ats_lever._classify reads <input type=radio> / <select> / <textarea>); that authoritative
# structural fact is carried as ctx.kind. We normalise the various adapter tags to ONE of the engine's
# fill paths so a choice card commits by CLICKING its already-visible option (S_CHOICE) and a custom
# dropdown opens+reads its options — BEFORE the label-meaning LLM mis-derives BOOLEAN/MULTI/SEARCH from
# the question wording (the proven Lever mis-route). GENERIC: matched on the adapter's own type tag,
# never a per-ATS string. An unknown/blank kind -> "" -> the engine falls through to today's classify.
def normalize_kind(kind: str) -> str:
    """Map an adapter control-type tag to a routing class: CHOICE | SELECT | TEXTAREA | DATE | "".

    CHOICE   = radio / checkbox (options already on the page -> S_CHOICE, click the match).
    SELECT   = single_select / multi_select / select / dropdown (custom dropdown -> open+read+commit).
    TEXTAREA = textarea / open_ended free-text box -> the text path.
    DATE     = a date control -> the date path.
    ""       = text / file / unknown -> no override; the engine classifies as today.
    """
    k = (kind or "").strip().lower()
    if not k:
        return ""
    if k in ("radio", "checkbox") or k.endswith("_radio") or k.endswith("_checkbox"):
        return "CHOICE"
    if "select" in k or k in ("dropdown", "combobox", "listbox"):
        # single_select / multi_select / native_select / dropdown — a closed option set behind a
        # (often custom) trigger. multi_select stays a list at the cardinality layer; routing-wise
        # both open+read the same way (the MULTI loop is driven by ctx.nature/cardinality downstream).
        return "SELECT"
    if k in ("textarea", "open_ended"):
        return "TEXTAREA"
    if k == "date":
        return "DATE"
    return ""


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


def _is_plain_text_editable_or_combo(node: Any) -> bool:
    """Can this control accept a filter KEYSTROKE? True for a plain text/textarea/contenteditable
    OR a combobox/searchbox/autocomplete (which ``_is_plain_text_editable`` deliberately vetoes).
    Used by the SELECT type-to-filter step (a custom dropdown's text input filters its option list).
    A native <select> is NOT keystroke-fillable here (it was already handled by read_options)."""
    if node is None:
        return False
    if _is_plain_text_editable(node):
        return True
    tag = _node_tag(node)
    if tag == "select":
        return False
    role = _node_role(node)
    if role in ("combobox", "searchbox"):
        return True
    if _node_attr(node, "aria-autocomplete") not in ("", "none"):
        return True
    # an <input> of a text-like type (even when a custom widget didn't set role) can take keystrokes.
    if tag == "input":
        typ = _node_attr(node, "type") or "text"
        return typ in ("text", "email", "url", "tel", "search", "")
    return False


def _is_click_only_combobox(node: Any) -> bool:
    """A combobox/listbox TRIGGER that cannot receive a filter keystroke: role=combobox/listbox or
    aria-haspopup=listbox, but NOT an <input>/<textarea>/contenteditable (a MUI Select div, a
    <button> that opens a listbox). Typing into it is a no-op — it must be READ + CLICKED via the
    aria/react-select reader, never type-to-filtered. STRUCTURAL (standard DOM, no renameable attr)."""
    if node is None:
        return False
    if _node_tag(node) in ("input", "textarea"):
        return False
    # only an EXPLICIT contenteditable excludes — _node_attr returns "" for an ABSENT attribute too,
    # so `in ("", "true")` wrongly excluded a plain MUI Select div (contenteditable absent -> "").
    if _node_attr(node, "contenteditable") == "true":
        return False
    return _node_role(node) in ("combobox", "listbox") or _node_attr(node, "aria-haspopup") == "listbox"


async def _probe_would_clobber(session: Any, ctx: "Ctx") -> bool:
    """True when typing a filter/search PROBE into ctx.node would DESTROY a sibling field's data.

    A probe types with clear=True. On a combobox the input IS the widget's filter box — always
    safe. On a PLAIN text input that already holds a non-empty value that is NOT this field's
    value, the located node is almost certainly the WRONG control (grouped/structure locate bound
    the question to a neighbour): hibob 'Country' landed on the First name input and typed
    'United States' over the committed 'Jordan'; verify then blessed the value-match — right
    value, wrong box (runs/newats/mega/20). Don't type; escalate."""
    node = ctx.node
    if _node_role(node) in ("combobox", "searchbox") or _node_attr(node, "aria-autocomplete") not in ("", "none"):
        return False
    if not _is_plain_text_editable(node):
        return False
    cur = ""
    with contextlib.suppress(Exception):
        from oa_dom_value import read_dom_value

        cur = ((await read_dom_value(session, node)) or "").strip()
    if not cur:
        return False
    return cur.lower() != (ctx.value or "").strip().lower()


def _filter_probes(value: str) -> list[str]:
    """Typeahead filter-query ladder, most-specific first: the FULL value (nails the exact row),
    then the value's last word (the distinguishing token — 'Berkeley'), then the old head-6 slice.
    The head-6 probe alone was the doordash mega4/15 school fail: 'Univer' matched every
    university on earth and the wanted row never entered the 58 rows read across two scrolls."""
    v = " ".join(str(value).split())
    if not v:
        return []
    toks = [t for t in re.split(r"[^\w]+", v) if len(t) >= 3]
    # HEAD comma-token: a geocomplete typeahead matches a CITY PREFIX, not the full "City, State,
    # Country" — typing 'San Francisco, CA, USA' yields 0 rows and the last-word fallback 'USA' pulls
    # garbage ('Uşak Turkey'). The leading comma part ('San Francisco') is the real query and surfaces
    # the right rows (mega6/16: 'San Francisco' -> 6 opts CORRECT vs the full string -> ESCALATE). Try
    # it right after the full value so location exits on it before the useless last-word probe.
    head_comma = v.split(",")[0].strip() if "," in v else ""
    cand = [v] + ([head_comma] if len(head_comma) >= 3 else []) + ([toks[-1]] if toks else []) + [v[: min(len(v), 6)]]
    seen: set[str] = set()
    out: list[str] = []
    for p in cand:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _identity_pick(value: str, options: list[str]) -> str | None:
    """Deterministic option match BEFORE any LLM pick: normalized equality, else a >=4-char
    word-boundary prefix either way; shortest hit wins. The LLM pick has refused EXACT matches
    (palantir 'Other' in a thousands-long list; airbnb NDA: the single filtered option was the
    wanted legal text verbatim and the pick still returned nothing)."""
    nv = " ".join((value or "").split()).lower()
    if not nv:
        return None
    hits = [o for o in options if " ".join(o.split()).lower() == nv]
    if hits:
        return min(hits, key=len)
    if len(nv) < 4:
        return None
    pref: list[str] = []
    for o in options:
        no = " ".join(o.split()).lower()
        if no.startswith(nv) and (len(no) == len(nv) or not no[len(nv)].isalnum()):
            pref.append(o)
        elif nv.startswith(no) and len(no) >= 4 and (len(nv) == len(no) or not nv[len(no)].isalnum()):
            pref.append(o)
    # AMBIGUITY -> None (doordash mega3/17: bare 'San Francisco' prefix-matched SIX geocomplete
    # rows and shortest-wins picked 'San Francisco, Cebu, Philippines' over California. A prefix
    # is identity evidence only when it is UNIQUE; multiple hits go to the LLM with the label.)
    if len(pref) == 1:
        return pref[0]
    if pref:
        return None
    # INITIALISM (stripe mega4/5 rerun: the country list offers 'US'/'UK' while the value is
    # 'United States' — the LLM pick refused and the required select died while a foreign
    # default stood). An option that IS the first-letters acronym of the value's words is the
    # value; unique hit only, same discipline as the prefix rule.
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", nv))
    if len(initials) >= 2:
        acr = [o for o in options if " ".join(o.split()).lower() == initials]
        if len(acr) == 1:
            return acr[0]
    return None


async def _chosen_plausible(ctx: "Ctx", chosen: str) -> bool:
    """ZERO-OVERLAP VETO (mega2/7 'Enter manually' committed for 'United States'; mega2/23
    'Toggle flyout' for 'LinkedIn'): a picked option sharing NO token with the wanted value is
    page chrome / a neighbour until proven otherwise. One memoized LLM confirm (the same
    pick_option) decides paraphrases ('Acknowledge' vs 'I understand that…' stays legal).
    The confirm also licenses a RESIDUAL want: when the mapper wants a generic catch-all
    ('Other'/'None of the above') it has no positive tokens to overlap a specific option, so the
    LLM permits the matching residual choice (twilio mega4/29,31 non-US role: source-of-right
    want='Other' resolved correctly to 'I do not currently have… will require sponsorship' but the
    old SAME-answer confirm compared it to the literal 'Other', vetoed it, and a context-blind
    retry then committed the WRONG 'Citizen or permanent resident')."""
    # CATCH-ALL WANT bypass (deterministic): the mapper emits a generic residual ('Other'/'None of
    # the above'/'N/A') when it is blind to the combobox OPTIONS at map-time; pick_option then
    # resolves it against the real read list. A residual carries no positive tokens to overlap a
    # SPECIFIC option, so this token+SAME-answer veto is structurally inapplicable — asking the LLM
    # 'is option-4 the same as \"None of the above\"?' reliably returns no and kills a correct pick.
    # Trust the pick_option resolution (twilio non-US source-of-right: 'None of the above'/'Other'
    # -> option 4 'I do not currently have… will require sponsorship', the truthful residual).
    if " ".join(re.findall(r"[a-z]+", (ctx.value or "").lower())) in _CATCH_ALL_WANTS:
        return True
    a = set(re.findall(r"[a-z0-9]+", (ctx.value or "").lower()))
    b = set(re.findall(r"[a-z0-9]+", (chosen or "").lower()))
    if a & b:
        return True
    # DIRECT equivalence ask, NOT pick_option([chosen]): a single-candidate pick invites the
    # closest-match bias — samsara mega4/24+26 blessed 'Gender Fluid' then 'Non-Binary' as the
    # only offered 'match' for want='Male'. Same-meaning is a yes/no question.
    with contextlib.suppress(Exception):
        from browser_use.llm.messages import UserMessage

        r = await ctx.llm.ainvoke([UserMessage(content=(
            f"A form answer should be: {ctx.value!r}\nThe option about to be committed reads: {chosen!r}\n"
            f"Field label: {(ctx.label or '')[:120]!r}\n"
            "Is committing this option a correct way to record that intended answer — i.e. EITHER they "
            "express the same answer (abbreviation, paraphrase, or format variant), OR the intended "
            "answer is a generic catch-all ('Other' / 'None of the above' / 'Not applicable' / 'Prefer "
            "not to answer') and this option is the matching residual choice for this field? "
            "A DIFFERENT specific choice from the same category is NOT acceptable. Reply exactly yes or no."
        ))])
        return str(getattr(r, "completion", r) or "").strip().lower().startswith("y")
    return False


def _occupied_is_own_field(ctx: "Ctx") -> bool:
    """RIGHT box, WRONG prefill (bamboohr mega/76-77: the Country combo ships with the tenant's
    'United Kingdom' default): the occupied control's OWN question-group text names THIS field —
    containment proves ownership, so overwriting the tenant default is the correct move, not a
    clobber. Same 0.5 bar as the locate containment gate."""
    with contextlib.suppress(Exception):
        gt = perc._group_text(ctx.node)
        return perc._overlap_score(perc._tokens(gt), perc._tokens(ctx.label or "")) >= 0.5
    return False


async def _rebind_empty_in_card(session: Any, ctx: "Ctx") -> Any | None:
    """The grouped/spatial tier bound an OCCUPIED foreign input while the question's REAL control
    sits EMPTY in the SAME card (ramp mega/49-50 payroll location: the card held both the occupied
    box it bound and the empty location combobox). Containment already proved the card — emptiness
    picks the virgin control: return the first OTHER empty keystroke-capable control in the card.
    None -> caller escalates as before."""
    if ctx.card is None:
        return None
    from oa_dom_value import read_dom_value

    own = getattr(ctx.node, "backend_node_id", None)
    for n in perc._controls_in(ctx.card):
        if getattr(n, "backend_node_id", None) == own:
            continue
        if not _is_plain_text_editable_or_combo(n):
            continue
        cur = ""
        with contextlib.suppress(Exception):
            cur = ((await read_dom_value(session, n)) or "").strip()
        if not cur:
            return n
    return None


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
    already strips; here we drop empties and de-dupe, preserving order (top-to-bottom).

    OPTION-SCOPE filter: a react-select re-render dumps CHROME (a 'Toggle flyout' / 'Clear
    selections' control) and NEIGHBOR question labels into the delta — those polluted the option
    set, so pick_option returned None and the field cycled to its deadline (robinhood: demographic
    selects each burning ~28s). Keep genuine option labels: role=option when present; else a short
    leaf label that is NOT a question (ends '?' / very long = a neighbor question) and NOT the
    trigger/button itself. Structural, not an exhaustive word list."""
    _EMPTY_MENU = re.compile(r"^no (options|results|matches)\b|^nothing found\b", re.IGNORECASE)

    def _in_menu_ancestor(node: Any) -> bool:
        p = node
        for _ in range(6):
            if p is None:
                return False
            attrs = getattr(p, "attributes", None) or {}
            if ((attrs.get("role") or "").lower() in ("listbox", "menu")
                    or "menu" in (attrs.get("class") or "").lower()):
                return True
            p = getattr(p, "parent_node", None)
        return False

    hard_chrome: set[int] = set()  # button/link chrome — must NEVER re-enter via the raw fallback

    def _is_option(d: perc.DeltaNode) -> bool:
        if _EMPTY_MENU.match((d.text or "").strip()):
            # the widget's empty-menu placeholder (react-select renders a literal 'No options'
            # row — samsara mega3/33 captured it as the single 'option' after a failed filter)
            return False
        role = ""
        with contextlib.suppress(Exception):
            ax = getattr(d.node, "ax_node", None)
            role = ((getattr(ax, "role", None) or "") if ax else "").lower()
            if not role:
                role = ((getattr(d.node, "attributes", None) or {}).get("role") or "").lower()
        if role == "option":
            return True
        tag = ""
        with contextlib.suppress(Exception):
            tag = perc._tag(d.node)
        if role in ("button", "link", "combobox", "textbox") or tag in ("button", "a"):
            # a button IS a legit option inside a menu container (pill/menu-item widgets);
            # outside one it is page chrome — samsara mega4/24 committed the literal
            # 'Enter manually' upload button as the years-of-experience answer because the
            # delta held ONLY chrome and the raw fallback let it back in.
            if _in_menu_ancestor(d.node):
                return True
            hard_chrome.add(id(d))
            return False
        t = (d.text or "").strip()
        if t.endswith("?") or len(t) > 80:
            return False  # a neighbor QUESTION swept into the delta, not an option
        return True

    seen: set[str] = set()
    out: list[str] = []
    scoped = [d for d in nodes if _is_option(d)]
    # if scoping removed everything, fall back to the SOFT-excluded raw nodes so an unusual menu
    # is not silently emptied — but hard chrome (buttons/links outside any menu) stays out.
    for d in scoped or [d for d in nodes if id(d) not in hard_chrome]:
        t = (d.text or "").strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _city_prefix(value: str) -> str:
    """The leading comma-token of a location-shaped value — 'San Francisco, CA, USA' -> 'San
    Francisco'. Returns '' when the value carries no comma (not location-shaped) so the geocomplete
    city-prefix variant only fires for an actual 'City, …' string. GENERIC: no place dictionary, no
    per-ATS rule — purely the first comma segment, which IS the city for a location field."""
    v = (value or "").strip()
    if "," not in v:
        return ""
    head = v.split(",")[0].strip()
    return head if head and head.lower() != v.lower() else ""


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
            kind=str(field.get("kind", "") or ""),
            resume=field.get("resume"),
            llm=field.get("llm"),
            adapter=field.get("adapter"),
            page=field.get("page"),
            field_obj=field.get("field_obj"),
            committed_nodes=field.get("_committed_nodes"),
        )
    )
    out = await _s0_guard(session, ctx)
    # PHANTOM double-bind guard: record the DOM id this field committed so a later phantom field that
    # re-binds the SAME node is skipped (S1_LOCATE checks the set). Only on a real commit.
    if out == DONE and ctx.committed_nodes is not None and ctx.loc_id:
        # record the id WITH the committer's QUESTION label. A later same-node hit is a real phantom twin
        # only if its label is a MUI placeholder ("Select an answer") or shares a token with THIS label;
        # a distinct field that merely grouped-desyncs onto this node (Ashby radio group -> the Name input)
        # shares nothing and must NOT be skipped. See _phantom_twin in _s1_locate.
        ctx.committed_nodes[ctx.loc_id] = _phantom_label(ctx.label)
    if not isinstance(field, Ctx):
        # surface the trace on the dict so the runner can record per-field detail
        field["_trace"] = ctx.trace
        field["_nature"] = ctx.nature
        field["_committed"] = ctx.committed_text
    return out


# ---- S0_GUARD ----
_SELFID_TOKENS = ("gender", "race", "ethnic", "hispanic", "latino", "disab", "veteran",
                  "self-identif", "sexual orientation", "transgender", "pronoun")


def _is_selfid(label: str) -> bool:
    """A demographic self-identification field (EEO/voluntary self-ID). Deliberately NARROW — excludes
    visa/sponsorship/consent/citizenship/age, which keep their own blank handling."""
    low = str(label or "").lower()
    return any(t in low for t in _SELFID_TOKENS)


# A required consent/agreement checkbox (GDPR gate, "I agree to the Terms", ...). STRUCTURAL gate:
# a checkbox-kind control whose label reads as an agreement/consent clause -- never a plain optional box.
_CONSENT_TOKENS = ("agree", "consent", "acknowledge", "terms", "privacy", "certify", "gdpr")


def _is_consent_checkbox(ctx: Ctx) -> bool:
    if "checkbox" not in str(getattr(ctx, "kind", "") or "").lower():
        return False
    low = str(ctx.label or "").lower()
    return any(t in low for t in _CONSENT_TOKENS)


# Toggle the field's real checkbox ON directly (bare arrow -- actor.page.evaluate calls (fn)() with no
# args; the name is embedded as a JSON literal). Targets the box by name/id, else the LONE required
# checkbox on the page; fires input+change so a submit-gate un-blocks. Returns .checked.
_CONSENT_CHECK_JS = r"""() => {
  const NAME = __NAME__;
  const boxes = [...document.querySelectorAll('input[type=checkbox]')];
  let el = null;
  if (NAME) el = boxes.find(b => b.name === NAME || b.id === NAME);
  if (!el) { const req = boxes.filter(b => b.required); if (req.length === 1) el = req[0]; }
  if (!el) return false;
  if (!el.checked) {
    try { el.checked = true; } catch(_) {}
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
  }
  return !!el.checked;
}"""


async def _commit_consent_checkbox(session: Any, ctx: Ctx) -> Outcome | None:
    """Directly check a required consent checkbox whose real <input> is sr-only (generic locate misses
    it). Self-verifying (returns .checked); None on any miss -> caller falls through to adapter/locate."""
    import json as _json
    with contextlib.suppress(Exception):
        page = await session.must_get_current_page()
        name = str(getattr(getattr(ctx, "field_obj", None), "name", "") or "")
        ok = await page.evaluate(_CONSENT_CHECK_JS.replace("__NAME__", _json.dumps(name)))
        if ok:
            ctx.committed_text = "Yes"
            ctx.trace.append("consent-check")
            return DONE
    return None


# ---- MEDIA-ANSWER PROBE (restraint class, clipboard 363): a question answered by RECORDING
# audio/video must NEVER be text-filled. STRUCTURAL + NARROW — it must NEVER net a document/resume
# UPLOAD (P4 rerun: the old bare `audio,video` match caught an AMBIENT page <video>/<audio> in a
# resume card's ancestry -> 51 false NEEDS_HUMAN on Resume/CV; a resume upload is a fillable field
# the engine hands test_resume.pdf, not a human-only recorder). Fires ONLY on a genuine capture
# control:
#   * input[type=file][capture]  (camera/mic capture), or
#   * input[type=file] whose accept is PURELY audio/*|video/*, or
#   * an EMPTY <audio>/<video> RECORD SINK (no src / no <source>) with NO file input in the card.
# EXCLUDES any card that carries a document/any-accept file input (a fillable upload, incl. resume)
# -> that takes the normal INTRINSIC_FILE path. accept-mime + file-input presence are the structural
# signals; a src'd ambient/promo media element is playback, never a recorder, and never fires.
_MEDIA_PROBE_JS = r"""() => {
  const el = document.getElementById(__REF__) || document.getElementsByName(__REF__)[0];
  if (!el) return 'no';
  // card = the field's OWN labeled group: walk up only while the parent adds NO other form field.
  // Stopping before a sibling input keeps a shared/ambient recorder bound to ITS answer field, not
  // every field on a small form (else one voice prompt blacks out all its siblings -> the same
  // net-negative class as the resume false-positives).
  let card = el, hops = 0;
  while (card.parentElement && hops++ < 6) {
    const p = card.parentElement;
    const others = [...p.querySelectorAll('input,select,textarea')].filter(x => x !== el && !card.contains(x));
    if (others.length) break;
    card = p;
  }
  const files = [...card.querySelectorAll('input[type=file]')];
  const avOnly = f => { const a = (f.getAttribute('accept') || '').toLowerCase().trim();
    return !!a && a.split(',').every(t => { t = t.trim(); return t.startsWith('audio/') || t.startsWith('video/'); }); };
  const isCapture = f => f.hasAttribute('capture') || avOnly(f);
  if (files.some(f => !isCapture(f))) return 'no';        // a doc/resume/any upload -> fillable, not media
  const hasCaptureInput = files.some(isCapture);
  const recordSink = [...card.querySelectorAll('audio,video')]
    .some(m => !m.getAttribute('src') && !m.querySelector('source'));  // empty sink = recorder, not playback
  return (hasCaptureInput || (recordSink && files.length === 0)) ? 'yes' : 'no';
}"""


async def _media_answer_field(session: Any, ctx: Ctx) -> bool:
    """True only when the field's card carries a genuine audio/video RECORDING control. A field that
    carries a resume payload or is typed as a file input is the fillable INTRINSIC_FILE path — never
    a media-answer field (the cheap early-out that kills the 51 Resume/CV false positives)."""
    if ctx.resume or str(getattr(ctx, "kind", "") or "").lower() in ("file", "input_file"):
        return False
    if ctx.page is None:
        return False
    ref = str(getattr(ctx.field_obj, "name", "") or "")
    if not ref:
        return False
    with contextlib.suppress(Exception):
        got = await ctx.page.evaluate(_MEDIA_PROBE_JS.replace("__REF__", json.dumps(ref)))
        return str(got).strip().lower() == "yes"
    return False


async def _s0_guard(session: Any, ctx: Ctx) -> Outcome:
    ctx.trace.append("S0_GUARD")
    # MEDIA-ANSWER field -> a human records the answer; the engine must not touch it (D-iii).
    if await _media_answer_field(session, ctx):
        ctx.trace.append("media-answer-field->NEEDS_HUMAN")
        return NEEDS_HUMAN
    # A file field carries its payload in ``resume`` (the value may be blank) — it is NOT a blank
    # field, so the blank->SKIP gate must not swallow it before the global file path runs.
    if not ctx.value.strip() and not ctx.resume:
        # REQUIRED self-identification with NO profile value: the correct answer is the explicit
        # 'Decline to self-identify' option, NOT a blank required field. Narrowly scoped to demographic
        # self-ID; the downstream select/choice path then matches the rendered decline option + verifies.
        if ctx.required and _is_selfid(ctx.label):
            ctx.value = "Decline to self-identify"
            ctx.trace.append("selfid-blank->decline")
        elif ctx.required and _is_consent_checkbox(ctx):
            # REQUIRED consent/agreement checkbox with NO value: the correct answer is AGREE (checked).
            # A blank required consent gate blocks submit; default to affirmative, committed just below.
            ctx.value = "Yes"
            ctx.trace.append("consent-blank->agree")
        else:
            ctx.trace.append("blank->SKIP")
            return SKIP
    # PROVEN-PATH FIRST: if this run has a per-archetype adapter, commit via its battle-tested
    # fill()+read_back() before the generic engine. DONE on a verified proven commit; otherwise
    # fall through to the generic locate/classify/commit as the fallback aid.
    # REQUIRED consent/agreement checkbox: its real <input> is often sr-only (1px / opacity:0), so the
    # generic locate returns no-control. Toggle it directly (self-verifying: reads back .checked) on an
    # affirmative value. Gated to required + checkbox kind + agreement label; never a plain optional box,
    # never an explicit decline. None -> fall through to adapter/locate unchanged.
    if ctx.required and _is_consent_checkbox(ctx) and brain._norm_lower(ctx.value) not in {"no", "false", "n", "decline"}:
        _cc = await _commit_consent_checkbox(session, ctx)
        if _cc is not None:
            return _cc
    proven = await _s_adapter(session, ctx)
    if proven is not None:
        return proven
    return await _s1_locate(session, ctx)


# ---- PROVEN-PATH DELEGATION (commit = adapter.fill, verify = adapter.read_back) ----
async def _s_adapter(session: Any, ctx: Ctx) -> Outcome | None:
    """Reuse the per-archetype adapter's proven interaction for ONE field.

    Returns DONE when the adapter commits a value its OWN read-back confirms; returns None to fall
    through to the generic engine (no adapter, file field, proven commit missed, or read-back
    unconfirmed). NEVER raises into the engine — a wedged proven CDP call is bounded and degrades
    to the generic path. This is the abstraction-layer reuse: the generic engine orchestrates
    (locate/verify/escalate/order/file/repeaters + VLM aid); the COMMIT delegates to proven code.
    """
    if ctx.adapter is None or ctx.page is None or ctx.field_obj is None:
        return None
    # File fields keep the dedicated GLOBAL file path (DOM.setFileInputFiles, absolute path,
    # ordered last) — the adapter's dropzone upload is the documented renderer-freeze. Let the
    # generic locate route ctx.resume to _s_file_global.
    if ctx.resume:
        return None
    ctx.trace.append("S_ADAPTER")
    try:
        ok = await asyncio.wait_for(
            ctx.adapter.fill(session, ctx.page, ctx.field_obj, ctx.value, None),
            timeout=ADAPTER_COMMIT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        ctx.trace.append("adapter.fill:timeout->generic")
        return None
    except Exception as exc:
        ctx.trace.append(f"adapter.fill:exc:{type(exc).__name__}->generic")
        return None
    if not ok:
        ctx.trace.append("adapter.fill:miss->generic")
        return None
    # VERIFY with the proven read_back — lenient for date/textarea/open-ended (_is_open_ended),
    # exact option-text for select/choice. Reuses the adapter's own oracle, not a generic rewrite.
    try:
        verified = await asyncio.wait_for(
            ctx.adapter.read_back(session, ctx.page, ctx.field_obj, ctx.value),
            timeout=ADAPTER_VERIFY_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        verified = False
    except Exception:
        verified = False
    if verified:
        ctx.committed_text = ctx.value
        ctx.trace.append("adapter:DONE")
        return DONE
    # Proven commit happened but its read-back was not confirmed -> let the generic verify/recommit
    # engine take over (it may confirm a value the adapter wrote, or re-commit a better option).
    ctx.trace.append("adapter.read_back:unconfirmed->generic")
    return None


# ---- S1_LOCATE ----
async def _s1_locate(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S1_LOCATE")
    # MAKE-REACHABLE (disabled-until-checkbox): a real-DOM-disabled target serializes as
    # non-interactive and cannot be typed, so locate drifts to a neighbour (82: the salary input
    # bound the gate checkbox -> INTRINSIC_CHECKBOX -> committed 'on', 145000 never landed). When
    # discovery gave a ref and that element is disabled with an unchecked, non-disabled checkbox in
    # its OWN container, tick the checkbox to enable it BEFORE we serialize + locate. Structural only
    # (disabled flag + sibling checkbox), no label text. Bare-arrow + inlined ref (ctx.page.evaluate
    # wraps arg as (fn)(); mirrors the scroll-into-view call below).
    _mrref = getattr(ctx.field_obj, "name", "") or ""
    if _mrref and ctx.page is not None:
        with contextlib.suppress(Exception):
            _mr = await ctx.page.evaluate(
                "() => { const el = document.getElementById(%s) || document.getElementsByName(%s)[0];"
                " if (!el || el.disabled !== true) return 'na';"
                " let p = el.parentElement, hops = 0;"
                " while (p && hops++ < 6) { const cb = [...p.querySelectorAll('input[type=checkbox]')]"
                ".find(c => !c.checked && !c.disabled); if (cb) { cb.click(); break; } p = p.parentElement; }"
                " let acted = false;"
                " if (el.disabled === true) {"  # DEPENDENT CASCADE: still disabled -> the IMMEDIATE
                # prerequisite <select> (State) already has a value but never fired its change at load.
                # Re-fire ONLY the LAST value-bearing select (the closest prerequisite) — firing an
                # UPSTREAM one (Country) can run a handler that RE-POPULATES + WIPES the downstream
                # select (keep=false), leaving the target disabled. The enable is often debounced, so
                # 'fired' proceeds (Python settles ~0.5s before locate).
                "   let q = el.parentElement, h2 = 0;"
                "   while (q && h2++ < 6) { const sels = [...q.querySelectorAll('select')].filter(s => s.value);"
                "     if (sels.length) { sels[sels.length - 1].dispatchEvent(new Event('change', {bubbles: true})); acted = true; break; } q = q.parentElement; } }"
                " return el.disabled === true ? (acted ? 'fired' : 'still-disabled') : 'enabled'; }"
                % (json.dumps(_mrref), json.dumps(_mrref))
            )
            if str(_mr) in ("enabled", "fired"):
                ctx.trace.append("make-reachable:enable-gate")
                await asyncio.sleep(0.55)  # a debounced cascade-enable (~300ms) needs a tick before locate
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
        # NO file input found. A field the discovery TYPED as a file field must NEVER fall through
        # to the text lane — that types the file PATH into some text box and dom-verifies its own
        # garbage as CORRECT (hibob: 'Resume*' -> S_TEXT -> verdict CORRECT while 'Add file' sat
        # empty; the false-complete the user caught). Native-picker-only uploaders are the HITL
        # class: report honestly.
        if str(getattr(ctx, "kind", "") or "").lower() in ("input_file", "file") or "file" in str(
            getattr(ctx, "source", "") or ""
        ).lower():
            ctx.trace.append("file-field-no-input->escalate")
            return ESCALATE if ctx.required else SKIP
        # else: fall through to generic locate (a resume path attached to a mis-tagged TEXT field).

    # FIX 1: tiered locate — STRUCTURE first, VISUAL PROXIMITY aid, GROUPED-WIDGET card-heading bind,
    # VLM disambiguate. Binds an unlabeled card input (Lever) the way a human does — by the question
    # text sitting near it; a non-text card (radio/checkbox/select/textarea) binds via its card heading.
    node, how, card = await perc.locate_field_tiered(
        state,
        ctx.label,
        vlm_pick=_make_vlm_pick(session, ctx),
        marks_pick=_make_marks_pick(session, ctx),
        dom_ref=getattr(ctx.field_obj, "name", "") or "",
    )
    # IDENTITY-RESCROLL: the dom-ref exists in the full DOM but not the viewport serialize (below
    # fold), and the ranked tiers produced only a WEAK bind — which is how a rating field bound the
    # phone country list (spatial hits a NEIGHBOUR). Scroll the identified element itself into view
    # and rebind by identity. One targeted scroll + one serialize, only when discovery gave a ref.
    # (includes how=='structure': had the ref'd element been in the viewport map, TIER 0 would
    # have bound it — so a similarity bind while the identity is off-viewport is suspect; live:
    # tail-differing rating labels made structure bind the NEIGHBOUR and verify read ITS value.)
    ref = getattr(ctx.field_obj, "name", "") or ""
    if ref and ctx.page is not None and how != "dom-ref":
        with contextlib.suppress(Exception):
            found = await ctx.page.evaluate(
                "() => { const el = document.getElementById(%s) || document.getElementsByName(%s)[0];"
                " if(!el) return false; el.scrollIntoView({block:'center'}); return true; }"
                % (json.dumps(ref), json.dumps(ref))
            )
            if str(found).lower() == "true":
                await asyncio.sleep(0.4)
                state = await perc.get_state(session)
                hit = perc._locate_by_dom_ref(state, ref)
                if hit is not None:
                    node, how, card = hit, "dom-ref-scrolled", None
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
        # NOT_RENDERED (D-ii, anthropic-004 class): the discovered element has NO rendered box AND
        # its card has no visible interactive either (a collapsed/display:none section) — the field
        # was never on screen, so it must not count as a fill failure. STRICT both-conditions check:
        # an sr-only input whose VISIBLE widget exists (react-select) keeps a rendered sibling and is
        # NOT marked (that is a locate failure we must keep honest). Trace-only; the runner excludes
        # 'not-rendered' rows from missing_required and marks the row.
        _nrref = str(getattr(ctx.field_obj, "name", "") or "")
        if _nrref and ctx.page is not None:
            with contextlib.suppress(Exception):
                _nr = await ctx.page.evaluate(
                    "() => { const el = document.getElementById(__REF__) || document.getElementsByName(__REF__)[0];"
                    " if (!el) return 'gone';"
                    " if (el.getClientRects().length) return 'rendered';"
                    " let card = el, hops = 0;"
                    " while (card.parentElement && hops++ < 5) {"
                    "   const p = card.parentElement;"
                    "   if (p.querySelectorAll('input,select,textarea').length > 4) break; card = p; }"
                    " const vis = [...card.querySelectorAll('input,select,textarea,button,[role=radio],[role=checkbox],[role=combobox]')]"
                    "   .some(e => e.getClientRects().length);"
                    " return vis ? 'rendered' : 'unrendered'; }".replace("__REF__", json.dumps(_nrref))
                )
                if str(_nr) == "unrendered":
                    ctx.trace.append("not-rendered")
        return ESCALATE if ctx.required else SKIP
    ctx.node = node
    ctx.card = card
    ctx.trace.append(f"located:{how}")
    # PHANTOM double-bind guard: the resolved node's stable DOM id (else the discovery dom-ref). If an
    # EARLIER field already committed THIS exact node, this is a phantom re-bind of the same widget (a
    # MUI Select native-mirror / a placeholder-labelled twin of the same control) — skip so its stray
    # value can't overwrite the real commit. Same-NODE (not same-label) so distinct fields are untouched.
    _lid = str((getattr(node, "attributes", None) or {}).get("id")
               or getattr(getattr(ctx, "field_obj", None), "name", "") or "")
    ctx.loc_id = _lid
    ctx.loc_how = how
    # Skip a phantom re-bind ONLY when the current field is a TRUE twin of the field that committed this
    # node — its label is a MUI placeholder ("Select an answer/option") or shares a content token with the
    # committer's question. A distinct radio group that grouped-DESYNCS onto an unrelated node (live Ashby:
    # 'visa' / 'how did you hear' resolve to the Name input's id) shares no token -> must commit, not skip.
    if _lid and ctx.committed_nodes is not None:
        _st = ctx.committed_nodes.get(_lid)
        if _st is not None and _st != "__widget__" and _phantom_twin(ctx.label, _st):
            ctx.trace.append("phantom-samenode->skip")
            return SKIP
    # WIDGET-BOUNDARY phantom guard: a composite widget (intl-tel) exposes a sub-control (the country
    # FLAG button) as a separate field whose node has NO id but sits INSIDE the widget container that an
    # earlier field already committed (its id was recorded by _split's dial path). Skip it so it can't
    # reset the widget. Only recorded widget-container ids are in the set, so a normal fieldset with two
    # real fields (different sub-ids, container not recorded) is untouched.
    if ctx.committed_nodes is not None:
        _cur = node
        for _ in range(5):
            _cur = getattr(_cur, "parent_node", None)
            if _cur is None:
                break
            _aid = str((getattr(_cur, "attributes", None) or {}).get("id") or "")
            # ONLY a container explicitly tagged "__widget__" by _split (intl-tel dial path) suppresses a
            # descendant — never an ordinary committed field's own node (that would nuke a sibling field
            # nested under a shared grouped ancestor).
            if _aid and ctx.committed_nodes.get(_aid) == "__widget__":
                ctx.trace.append("phantom-widget->skip")
                return SKIP
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
    # LABEL-ANCHORED JUMP first: the page can be many viewports tall (1password mega4/1: two
    # viewport steps never reached the bottom questions and browser-use's selector_map only
    # carries the viewport's surroundings). The field's OWN label text is a known anchor —
    # scrollIntoView it in one jump, then re-locate. Viewport stepping stays as the fallback.
    with contextlib.suppress(Exception):
        page = await session.must_get_current_page()
        hit = await page.evaluate(
            "(lab) => { const norm=s=>(s||'').replace(/\\s+/g,' ').trim().toLowerCase();"
            " const want=norm(lab).slice(0,80); if(!want || want.length<6) return false;"
            " for(const e of document.querySelectorAll('label,legend,div,span,p,h1,h2,h3,h4')){"
            "   if(e.children.length>8) continue; const t=norm(e.innerText); if(!t) continue;"
            "   if(t.startsWith(want) || (t.length>10 && want.startsWith(t.slice(0,60)))){"
            "     e.scrollIntoView({block:'center'}); return true; } } return false; }",
            str(ctx.label)[:200],
        )
        if hit:
            state = await perc.get_state(session)
            node, how, card = await perc.locate_field_tiered(
                state,
                ctx.label,
                vlm_pick=_make_vlm_pick(session, ctx),
                marks_pick=_make_marks_pick(session, ctx),
                dom_ref=getattr(ctx.field_obj, "name", "") or "",
            )
            ctx.trace.append(f"label-jump:{'hit' if node is not None else 'miss'}")
            if node is not None:
                return (node, how, card, state)
    while ctx.scroll_reads < SCROLL_CAP:
        ctx.scroll_reads += 1
        await act.scroll(session, None, _SCROLL_PX)
        state = await perc.get_state(session)
        node, how, card = await perc.locate_field_tiered(
            state,
            ctx.label,
            vlm_pick=_make_vlm_pick(session, ctx),
            marks_pick=_make_marks_pick(session, ctx),
            dom_ref=getattr(ctx.field_obj, "name", "") or "",
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


async def _s_grid_or_scale(session: Any, ctx: Ctx) -> Outcome | None:
    """NEW COMMIT RUNG — a Likert MATRIX CELL or an ordinal RATING SCALE, the two grouped widgets whose
    option can only be bound by STRUCTURAL association (aria-labelledby row x column) or ORDINAL position:
      * Likert: the shared column label ('Strongly agree') repeats in every row, so the text-matching
        choice path checks the WRONG row's radio and the tested group reads BLANK. Bind the cell by
        rowHeader-text == this field's label AND colHeader-text == value.
      * Star rating: N sibling <button>/[role=radio] in a [role=group] labelled by this field's label; the
        integer value is the ORDINAL, and the options carry the star glyph (never the digit) so text/label
        matching finds nothing. Click the Nth.
    Self-verifying (real .checked / aria-pressed state), so it can never false-green. Returns None for every
    field that is NOT one of these two structures -> classify continues unchanged (radio/checkbox/select hot
    path untouched). A matched-but-uncommitted structure is reported honestly (escalate/skip) rather than
    dropped to the text path, which would false-green off the repeated column label."""
    if not ctx.value or ctx.resume:
        return None
    # Cheap gate: only choice-ish controls pay the probe; text/select/date fields return here untouched.
    _tag = _node_tag(ctx.node)
    _role = _node_role(ctx.node)
    if (
        classify_intrinsic(ctx.node) != "INTRINSIC_RADIO"
        and normalize_kind(ctx.kind) != "CHOICE"
        and _tag != "button"
        and _role not in ("radio", "button", "group", "radiogroup")
    ):
        return None
    with contextlib.suppress(Exception):
        _pg = await session.must_get_current_page()
        committed, matched = await cdpa.cdp_commit_grid_or_scale(_pg, ctx.label, ctx.value)
        if committed:
            ctx.nature = "INTRINSIC_RADIO"
            ctx.committed_text = committed
            ctx.trace.append(f"grid-scale-commit:{committed[:20]}")
            return DONE
        if matched:
            # the exact cell/scale WAS found but the click didn't take — do NOT fall to the text path
            # (it would check the shared column label in the wrong row / miss the ordinal). Report honestly.
            ctx.trace.append("grid-scale:matched-uncommitted")
            return ESCALATE if ctx.required else SKIP
    return None


# ---- S2_CLASSIFY ----
async def _s2_classify(session: Any, ctx: Ctx, state: perc.OAState | None = None) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S2_CLASSIFY")
    # ALREADY-CORRECT no-op (idempotence): read the bound control's CURRENT value through the SAME
    # DOM-first oracle verify uses (VLM off — free) BEFORE any interaction. A prefilled-correct
    # widget must never be touched: opening/clicking the phone country list to "set" the +1 it
    # already had flipped it to +44 and invalidated the phone (live wk16). ONLY for the open/click
    # lanes (CHOICE/SELECT) — that is where touching breaks state; a text control is idempotent to
    # re-type, and its pre-check false-positived live (LLM matched the iti '+1' residue against the
    # phone number -> phone left empty, wk17). EMPTY/UNKNOWN/WRONG falls through unchanged; no
    # verify budget is spent on the pre-check.
    # TRUST GATE: the pre-check may only short-circuit when the LOCATE bound the field's OWN single
    # control (dom-ref / structure / accessible-name). A spatial locate can drift to a NEIGHBOR; a
    # GROUPED locate binds a REPRESENTATIVE node whose DOM value desyncs from the painted control —
    # duolingo sponsorship: verify read a stale 'No' off the representative while the react-select
    # painted 'SELECT…', short-circuited, committed nothing, reverted (rv3, live-confirmed the veto
    # below can't even resolve a grouped representative). Grouped choice widgets are idempotent to
    # re-commit, so excluding them only costs a re-click on the rare genuinely-prefilled grouped field.
    _loc_trusted = any(t in ctx.trace for t in ("located:dom-ref", "located:structure"))
    if ctx.value and not ctx.resume and _loc_trusted and normalize_kind(ctx.kind) in ("CHOICE", "SELECT"):
        with contextlib.suppress(Exception):
            pre = await brain.verify(
                session,
                ctx.label,
                ctx.value,
                node=ctx.node,
                llm=ctx.llm,
                key=f"pre:{ctx.label}",
                use_cache=True,
                allow_vlm=False,
            )
            if pre == "CORRECT":
                # RENDER VETO (rv3 sponsorship): DOM/verify said CORRECT off a react-select's stale
                # .value while the CONTROL paints only 'SELECT…' — so the field is skipped as prefilled,
                # committed nothing, and reverts (a false-green the render gate later flags). Confirm the
                # value is actually PAINTED before trusting the short-circuit. Runs for CHOICE and SELECT
                # alike (sponsorship classifies BOOLEAN, not SELECT — a SELECT-only gate never saw it):
                # the reader is the discriminator — it fires ONLY on a real placeholder, which a true
                # radio/checkbox never paints, so their idempotence protection (phone +1->+44) is intact.
                _ph = False
                with contextlib.suppress(Exception):
                    _ph = await cdpa.rendered_is_placeholder(session, ctx.node, ctx.label, ctx.value)
                if not _ph:
                    ctx.committed_text = ctx.value
                    ctx.trace.append("already-correct")
                    return DONE
                ctx.trace.append("already-correct-veto:placeholder-rendered")
    # MATRIX / SCALE (structural-association commit): a Likert grid cell or an ordinal rating scale can
    # only be bound by row/col header association (aria-labelledby) or ordinal position — free option-text
    # matches the SHARED column label in the WRONG row (Likert) or never equals the star glyph (rating).
    # Runs BEFORE intrinsic/kind routing so the text-matching S_CHOICE never gets a chance to false-green
    # off the repeated column label. Self-gating: returns None for every non-grid/non-scale field, so the
    # normal radio/checkbox/select hot path is untouched.
    _gs = await _s_grid_or_scale(session, ctx)
    if _gs is not None:
        return _gs
    intrinsic = classify_intrinsic(ctx.node)
    if intrinsic:
        ctx.nature = intrinsic
        ctx.trace.append(f"intrinsic:{intrinsic}")
        if intrinsic == "INTRINSIC_FILE":
            return await _s_file(session, ctx)
        if intrinsic in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
            # MULTI checkbox group: a multi_select whose value is a LIST (Skills/Languages = 'Python, Go')
            # must check EVERY matching box. INTRINSIC_CHECKBOX alone routed it to the single-pick _s_choice
            # which committed only the head token 'Python' and dropped 'Go' (widget-zoo multi FAIL). Route a
            # genuine multi-value checkbox group to the per-value loop instead.
            if intrinsic == "INTRINSIC_CHECKBOX" and ("," in ctx.value or ";" in ctx.value) and (
                str(getattr(ctx.field_obj, "type", "") or "").lower() == "multi_select"
                or brain.is_multi_label(ctx.label)
                or ctx.cardinality == "many"
            ):
                return await _s_multi_checkbox(session, ctx, state)
            # SNAPSHOT REUSE: the radio/checkbox options are STATIC siblings already present in the
            # locate snapshot (no click reveals them) — pass it through so _s_choice reads the group
            # from the SAME serialize, never a second full-page get_state per choice field.
            return await _s_choice(session, ctx, state)
        if intrinsic == "INTRINSIC_SELECT":
            # an ARIA role=listbox (not a native <select>) — cdp_select/select_option no-op on it;
            # click its [role=option] cells by text instead (single + aria-multiselectable).
            if _node_tag(ctx.node) != "select":
                return await _s_listbox(session, ctx)
            return await _s_native(session, ctx)
        if intrinsic == "INTRINSIC_DATE":
            return await _s_date(session, ctx)

    # KIND-HINT route (the card-commit fix) — runs AFTER intrinsic (a true <input type=radio> /
    # <select> is the strongest signal) but BEFORE the label-meaning LLM. The live Lever radios /
    # custom selects often DON'T expose a standard type/role on the representative node the locate
    # bound (a styled <label>-wrapped input, a button-trigger custom dropdown), so classify_intrinsic
    # returns "" and the label-LLM mis-derives BOOLEAN/MULTI/SEARCH -> S3_OPEN/S4_SEARCH -> 0 opts ->
    # ESCALATE (runs/final3/lever.json). The adapter ALREADY parsed the real control type; honour it.
    routed = normalize_kind(ctx.kind)
    if routed:
        ctx.trace.append(f"kind-hint:{ctx.kind}->{routed}")
        if routed == "CHOICE":
            # radio/checkbox: the options are ALREADY VISIBLE in the located card. Read the group
            # (scoped to ctx.card) and CLICK the matching option — never open a dropdown / type-search.
            # Set nature to the ADAPTER'S intrinsic kind so the whole reliable-choice-commit verify
            # path (DOM read-back primary, accept-on-UNKNOWN, recommit-by-click) applies unchanged.
            ctx.nature = _kind_to_intrinsic(ctx.kind) or "INTRINSIC_RADIO"
            return await _s_choice(session, ctx, state)
        if routed == "SELECT":
            # ARIA-DISCLOSURE SELECT via CDP-CORE (duolingo: <button aria-haspopup=listbox>).
            # S3's delta-open path is built for react-select inputs; the disclosure BUTTON opens
            # only on a trusted click and reads via aria-controls — exactly what core._open +
            # core.choose_option do (live-proven on duolingo: readback 'NO'). Try it FIRST for
            # aria-haspopup nodes; fall through to S3 if it can't resolve/commit.
            # aria-haspopup can be on ctx.node OR a descendant button — duolingo binds the
            # role=group WRAPPER (discovery name=group.id) whose disclosure button carries the
            # attribute, so a node-only check misses it. core._trigger descends to the button;
            # here we just gate on the attribute appearing at or below the node.
            def _haspopup_at_or_below(n: Any, depth: int = 0) -> bool:
                if n is None or depth > 4:
                    return False
                if (_node_attr(n, "aria-haspopup") or "").lower() in ("listbox", "menu"):
                    return True
                for k in (getattr(n, "children_nodes", None) or [])[:20]:
                    if _haspopup_at_or_below(k, depth + 1):
                        return True
                return False

            # CORE-FIRST for every custom SELECT (duolingo mega4/20: the office question's
            # perception-tree haspopup check was unreliable — locate bound a filter textbox whose
            # subtree lacked the sibling button, so the gate missed it and it fell to S3 ->
            # commit-cap). core.read_options resolves by the group NAME and descends to the real
            # trigger (button OR react-select input) regardless of which control locate bound;
            # it safely returns [] when it cannot open/read, falling through to S3. So there is no
            # need to pre-detect the widget kind — try core whenever there is a resolvable name.
            # (Native <select> already short-circuited via classify_intrinsic before this route.)
            _core_name = str(getattr(ctx.field_obj, "name", "") or "")
            if _core_name:
                with contextlib.suppress(Exception):
                    import oa_cdp_core as core

                    _opts = await core.read_options(session, _core_name)
                    if _opts:
                        _chosen = _identity_pick(ctx.value, _opts) or await brain.pick_option(
                            ctx.value, _opts, llm=ctx.llm, label=ctx.label)
                        if _chosen and await _chosen_plausible(ctx, _chosen):
                            _got = await core.choose_option(session, _core_name, _chosen)
                            if _got:
                                ctx.nature = "CLOSED_LIST"
                                ctx.committed_text = _got
                                ctx.trace.append(f"aria-core-commit:{_got[:20]}")
                                return await _s_verify(session, ctx)
            # single/multi-select: a (often custom) dropdown — open + read its options + commit the
            # match. Lever's custom select renders options only after a click, so S3_OPEN's click+
            # settle+delta path is exactly right; a native <select> short-circuits via read_options.
            ctx.nature = "MULTI" if ctx.cardinality == "many" else "CLOSED_LIST"
            return await _s3_open(session, ctx)
        if routed == "TEXTAREA":
            ctx.nature = "FREE_TEXT"
            return await _s_text_guard(session, ctx)
        if routed == "DATE":
            ctx.nature = "DATE"
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
    # TAG/CHIP input: a MULTI on a PLAIN text input (not a combobox typeahead) is a chip box — type each
    # value + Enter, no option menu to search. Route straight to the multi loop (nothing committed yet).
    if nature == "MULTI" and _is_plain_text_editable(ctx.node):  # plain text (helper excludes comboboxes)
        return await _s_multi_loop(session, ctx, first_committed=False)
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
    # the visible label on multi-upload forms is a generic verb ('Attach' twice on airbnb —
    # resume AND cover letter); the discovery NAME is the only distinguishing identity, so
    # hand the picker both (mega4/9: want='Attach' picked the resume row, the cover-letter
    # required upload silently landed on the already-filled resume input).
    _fname = str(getattr(ctx.field_obj, "name", "") or "")
    _want = f"{ctx.label} ({_fname})" if _fname and _fname.lower() not in ctx.label.lower() else ctx.label
    # FRESH state: attaching the SIBLING uploader re-renders the section and consumes its
    # <input> (live CDP on the greenhouse embed: #resume disappears from the DOM once set)
    # — a snapshot from before that render binds a STALE node and the set lands nowhere
    # while the global verify sees the sibling's chip (airbnb mega4/11 false-DONE pair).
    with contextlib.suppress(Exception):
        state = await perc.get_state(session)
    node = await filoc.find_file_input(state, _want, llm=ctx.llm)
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
    # DIRECT-CDP setFileInputFiles FIRST (no event-bus UploadFileEvent, whose readiness wait hangs on a
    # busy SPA — the Ashby cover-letter 2nd-dropzone HARD-FIELD-TIMEOUT). Fall back to the event-bus path.
    ok = await cdpa.cdp_set_file(session, node, str(path))
    if not ok:
        with contextlib.suppress(Exception):  # bounded: the event-bus upload hangs on some SPA dropzones
            ok = await asyncio.wait_for(act.upload_file(session, node, str(path)), timeout=10.0)
    if not ok:
        ctx.trace.append("upload-failed")
        return ESCALATE if ctx.required else SKIP
    # UI-VERIFY: a CDP set can land on a DECOY hidden input while the visible uploader (hibob's
    # custom 'Add file') never reflects it — that was a FALSE-DONE (json 'uploaded' but the page
    # showed 'Add file' empty). Confirm the filename actually renders before claiming success.
    # NOTE: the node-scoped card verify was too strict — on stripe both file inputs share an
    # ancestor, so the 'stop at >1 file input' boundary excluded a chip that WAS rendered, turning a
    # correctly-attached resume into a false-negative escalate (mega4_val/7: both files visibly
    # attached, ledger said uploaded-but-not-in-ui). The airbnb resume->cover false-green needs a
    # SPATIAL per-dropzone verify (live-CDP TODO); page-global until then (better a rare false-green
    # than regressing every multi-upload form to a false-negative).
    if not await _file_visible_in_ui(session, str(path)):
        ctx.trace.append("uploaded-but-not-in-ui->escalate")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = str(path)
    ctx.trace.append("uploaded+ui-verified")
    return DONE


async def _file_visible_in_ui(session: Any, path: str, node: Any = None) -> bool:
    """True when the uploaded file's basename renders as visible text (deep/shadow-pierced) OR a
    connected file input actually holds it. Distinguishes a REAL upload from a CDP set that landed
    on a decoy input the visible widget ignores. Best-effort True on read failure (don't regress a
    working upload on a transient read error).

    When ``node`` (the target file input) is given, the search is scoped to ITS card first: on a
    multi-upload form every field gets the SAME file, so a page-global filename search is vacuously
    true once the resume chip renders anywhere (airbnb mega4/9: cover-letter upload landed on the
    resume input, global verify saw the resume chip, ledger claimed 'uploaded+ui-verified')."""
    import os as _os

    base = _os.path.basename(str(path))
    needle = base[:8].lower()  # 'test_res' — matches full + truncated 'test_res…pdf' renders
    if node is not None:
        _CARD_CHIP_JS = r"""
function(n){
  try{
    let p = this.parentElement;
    for(let i=0;i<8&&p;i++){
      // crossing into a container with a SECOND file input = past our own card
      if(p.querySelectorAll('input[type=file]').length>1) break;
      if(((p.innerText||'').toLowerCase()).includes(n)) return true;
      p = p.parentElement;
    }
    return false;
  }catch(e){ return false }
}
"""
        with contextlib.suppress(Exception):
            r = await cdpa._resolve(session, node)
            if r is None:
                # an unresolvable node is a STALE bind (the widget consumed its <input> on the
                # sibling's attach) — falling to the page-global scan here is what blessed the
                # airbnb false-DONE pair (the sibling's chip carries the same filename).
                return False
            cdp_session, session_id, object_id = r
            got = await cdpa._call_on(cdp_session, session_id, object_id, _CARD_CHIP_JS, args=[needle])
            if isinstance(got, bool):
                return got  # card-scoped verdict is authoritative; inconclusive falls through
    with contextlib.suppress(Exception):
        page = await session.must_get_current_page()
        # VISIBLE filename text only — NOT input.files, which the decoy hidden input also holds
        # (that is the exact false-positive: a real uploader RENDERS the name, a decoy does not).
        # STRING SENTINEL, never bool(evaluate): the serialized return made bool("false") True,
        # so this UI-verify was silently fail-open (same trap as the captcha gate).
        found = await page.evaluate(
            "(n) => { const all=[]; const walk=(r)=>{ for(const e of r.querySelectorAll('*')){ all.push(e);"
            "   if(e.shadowRoot) walk(e.shadowRoot); } }; walk(document);"
            " for(const e of all){ if(e.childNodes.length && (e.innerText||'').toLowerCase().includes(n)){"
            "   const r=e.getBoundingClientRect(); if(r.width>0 && r.height>0) return 'yes'; } } return 'no'; }",
            needle,
        )
        return str(found).strip().lower() == "yes"
    return True  # read failed -> don't punish a likely-good upload


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
    # UI-VERIFY the filename actually renders (a CDP set can land on a decoy input the visible
    # widget ignores — the hibob false-DONE); presence-only trust was the bug.
    if not await _file_visible_in_ui(session, str(path), node=ctx.node):
        ctx.trace.append("uploaded-but-not-in-ui->escalate")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = str(path)
    return DONE


# --------------------------------------------------------------------------- #
# VISUAL COMMIT FALLBACK (the LAST gap) — when the DOM option-read is EMPTY but the options
# are VISIBLE on screen (Lever styled-div radios; a custom single_select / geocomplete whose
# options render in a portal the delta misses). SCREENSHOT + set-of-marks over the visible
# candidate option elements + VLM pick-by-VALUE -> click the chosen option BY COORDINATE.
# GENERIC (no per-ATS string), FILL-ONLY, and a STRICT FALLBACK: it only runs when the DOM read
# yielded nothing, so it never touches a standard widget that read its options fine. Bounded by
# the SAME per-field VLM sub-budget (FIELD_VLM_CAP) the verify oracle + locate tiers share.
# --------------------------------------------------------------------------- #
async def _visual_commit(session: Any, ctx: Ctx, candidates: list[Any]) -> bool:
    """SEE the rendered options + click the one meaning ctx.value BY COORDINATE. Returns True on a
    committed click (ctx.committed_text set), False when the VLM finds nothing / budget spent / no box.

    Reuses the EXISTING set-of-marks helper (``brain.pick_option_by_marks`` -> ``vision_verify``'s
    screenshot+counter) and the EXISTING coordinate click (``act.click_xy`` -> ``cdp_click_xy``); it
    adds no new perception/action — only the routing that the visible options ARE the option set when
    the DOM read came back empty. Spends at most ONE VLM aid from the per-field sub-budget."""
    cands = [c for c in candidates if c is not None and getattr(c, "backend_node_id", None) is not None]
    if not cands:
        return False
    if ctx.vlm_used >= FIELD_VLM_CAP:
        ctx.trace.append("visual-commit-budget")
        return False
    before_n = _vv_calls()
    chosen = None
    try:
        chosen = await brain.pick_option_by_marks(session, ctx.value, cands, llm=ctx.llm)
    except Exception:
        chosen = None
    spent = max(0, _vv_calls() - before_n)
    if spent:
        ctx.vlm_used += spent
    if chosen is None:
        ctx.trace.append("visual-choice:none")
        return False
    if perc.node_center(chosen) is None:
        ctx.trace.append("visual-choice:no-box")
        return False
    # COMBINE visual + DOM: the VLM said WHICH option (semantics), now the DOM gives the precise
    # CLICKABLE target. The VLM often marks the option's TEXT span, but a styled-div radio only toggles
    # when its real interactive element (the radio input / the <label> / the clickable option ROW) is
    # clicked — clicking the text center misses the hotspot (the observed Lever failure). Resolve the
    # chosen option to that element, then issue a TRUSTED direct-CDP click on it (real mouse event +
    # elementFromPoint reroute + JS .click() fallback fires the React handler regardless of coordinate).
    target = _resolve_choice_target(chosen, candidates)
    _chosen_text = perc.node_label_text(chosen) or perc.node_option_text(chosen) or ctx.value
    # SELF-LABEL REJECT (discord mega/30): when the VLM's pick IS the field's own question/control
    # (chosen text == our label), clicking it OPENS the widget — it commits nothing. Recording it
    # as committed let verify read the transient typed text as CORRECT while blur reverted the
    # widget to its placeholder ('How did you hear about this job?' became the committed 'value').
    # Reject BEFORE clicking (the click would just leave a menu hanging open). A CHOICE group is
    # exempt: a lone radio/checkbox legitimately carries the question as its accessible label and
    # self-click IS the commit (offline radio cases).
    # strip the trailing required-marker: the on-page label renders 'What is your notice
    # period?*' while discovery's label has no star — the one-char tail let the reject slip
    # (airwallex mega/38 committed the question WITH the star as its 'value').
    _nlab = " ".join((ctx.label or "").split()).lower().rstrip("*: ")
    _nch = " ".join(_chosen_text.split()).lower().rstrip("*: ")
    if _nlab and _nch == _nlab and classify_intrinsic(target) not in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
        ctx.trace.append("visual-choice:self-label-reject")
        return False
    if not await _chosen_plausible(ctx, _chosen_text):
        ctx.trace.append("visual-choice:implausible-reject")
        return False
    ok = await act.click_node(session, target)
    if not ok:
        ok = await act.click_node_center(session, target)  # coordinate fallback
    if not ok:
        ctx.trace.append("visual-choice:click-failed")
        return False
    ctx.committed_text = _chosen_text
    ctx.node = target  # the verify oracle reads back the option control we actually clicked
    how = "self" if target is chosen else "resolved"
    ctx.trace.append(f"visual-choice+cdp_click:{how}")
    return True


def _resolve_choice_target(chosen: Any, candidates: list[Any]) -> Any:
    """COMBINE: given the VLM-chosen option node (often its TEXT), return the real CLICKABLE element to
    hit. Prefer (1) the chosen node if it is itself a radio/checkbox; (2) a radio/checkbox candidate in
    the SAME horizontal row band (the styled circle sits left of the text); (3) the chosen node's nearest
    interactive ancestor (the <label>/clickable option ROW that wraps circle+text); (4) the chosen node.
    Pure DOM geometry/structure — generic, no per-ATS hook."""
    if classify_intrinsic(chosen) in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
        return chosen
    cbox = perc.node_rect(chosen)
    if cbox is not None:
        cy = cbox[1] + cbox[3] / 2.0
        # (2) a real radio/checkbox option control on the same row as the chosen text.
        for n in candidates:
            if n is chosen or classify_intrinsic(n) not in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
                continue
            nb = perc.node_rect(n)
            if nb is not None and abs((nb[1] + nb[3] / 2.0) - cy) <= 16:
                return n
    # (3) the nearest interactive ancestor (the clickable option row / <label> wrapping circle+text).
    parent = getattr(chosen, "parent_node", None)
    hops = 0
    while parent is not None and hops < 4:
        tag = (getattr(parent, "node_name", "") or "").lower()
        role = (getattr(getattr(parent, "ax_node", None), "role", None) or "").lower()
        attrs = getattr(parent, "attributes", None) or {}
        if (
            tag == "label"
            or role in ("radio", "checkbox", "option", "button")
            or attrs.get("role")
            in (
                "radio",
                "checkbox",
                "option",
                "button",
            )
        ):
            return parent
        parent = getattr(parent, "parent_node", None)
        hops += 1
    return chosen


# ---- S_CHOICE (radio / checkbox; options already on screen) ----
async def _s_choice(session: Any, ctx: Ctx, state: perc.OAState | None = None) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_CHOICE")
    # GROUND-TRUTH-FIRST (顶层设计 — the ONE widget-agnostic commit): re-resolve the field by its own
    # LABEL (immune to a grouped-locate desync that binds the WRONG node) and accept ONLY on the REAL
    # painted selected-state — a pill's active class, a native radio's .checked — never label text, never
    # the bound node's identity. Self-verifying, so it cannot false-green. Covers ashby pill <button>s AND
    # native <input type=radio> alike (AfterQuery 'authorized to work' — ledger DONE, radio blank on screen
    # — was the desync this closes). Runs BEFORE the structural scan, which trusts the located container and
    # so inherits the desync; that scan is now only the FALLBACK for controls with no label-anchorable
    # button/radio (e.g. Lever's styled <div> whose hidden <input> its own <label> reflects).
    # ALWAYS try the label-anchor first inside _s_choice (this IS the choice handler). It self-verifies
    # on real selected-state and returns "" on no match, so an unconditional attempt cannot false-green
    # and cannot regress — while a narrow kind/nature gate silently skipped it for a segmented/toggle
    # button group (kind-hint radio, no native inputs) -> choice-no-group -> S_OTHER_GUARD -> SKIP.
    _la_found = False
    if True:
        with contextlib.suppress(Exception):
            _pg = await session.must_get_current_page()
            _btn, _la_found = await cdpa.cdp_choose_button_by_label(_pg, ctx.label, ctx.value)
            if _btn:
                ctx.committed_text = _btn
                ctx.trace.append(f"choice-label-anchor:{_btn[:20]}")
                if _reveal_pending_text(ctx.value, _btn):
                    return await _s_cascade(session, ctx)  # 'Yes' revealed a 'please specify' input, etc.
                return DONE

    # FALLBACK — structural DOM-direct (the proven Lever ._click_option, generalised): scan the located
    # container's REAL radio/checkbox inputs, INCLUDING visually-hidden styled ones (Lever hides the real
    # <input value="Yes"> behind a styled <label>), match the input meaning ctx.value, .click() + fire
    # input/change. Reached only when the label-anchor found NO button/radio for this field.
    container = ctx.card if ctx.card is not None else ctx.node
    if container is not None:
        matched = await cdpa.cdp_choose_option(
            session, container, ctx.value, group_name=getattr(ctx.field_obj, "name", "") or ""
        )
        if matched:
            if _la_found:
                # The label-anchor DID find a matching control for this field but it was NOT selected, yet
                # the structural scan now claims a match — that match is the grouped-locate desync
                # false-green (committed on the wrong node). Do NOT trust it: fall through to the
                # group-read / visual path, which clicks the correct option and ends in _s_verify (real state).
                ctx.trace.append("choice-falsegreen-caught")
            else:
                # No label-anchorable control existed (Lever styled-DIV whose hidden input its own label
                # reflects, verified by cdp_choose_option's _STILL_CHECKED): trust the matched click.
                ctx.committed_text = matched
                ctx.trace.append(f"choice-dom-direct:{matched[:20]}")
                return DONE

    # The group's options are siblings already rendered. SNAPSHOT REUSE: prefer the locate snapshot
    # handed down from classify (the static radio/checkbox group is already in it) — only serialize
    # afresh if the caller had none (e.g. a re-entry). This removes a full-page get_state per choice
    # field, a primary cost on a heavy SPA.
    if state is None:
        state = await perc.get_state(session)
    group = _read_choice_group(state, ctx)
    if not group:
        ctx.trace.append("choice-no-group")
        # DUAL-LISTBOX transfer widget (two item panels + a move/add button, no radio/checkbox group):
        # move each value from the source list to the selected panel. Gated on that structure, so a
        # normal choice never reaches it. _s_verify re-checks the Selected panel -> cannot false-green.
        with contextlib.suppress(Exception):
            _pg = await session.must_get_current_page()
            _parts = [p.strip() for p in ctx.value.replace(";", ",").split(",") if p.strip()] or [ctx.value]
            _dn = await cdpa.cdp_dual_listbox_transfer(_pg, ctx.label, _parts[:MULTI_CAP])
            if _dn and _dn > 0:
                ctx.committed_text = ", ".join(_parts[:MULTI_CAP])
                ctx.trace.append(f"dual-listbox:{_dn}")
                return await _s_verify(session, ctx)
        # VISUAL FALLBACK (preferred over the standalone-checkbox heuristic): the DOM read found NO
        # option controls (Lever radios are styled DIVs, not <input type=radio> — invisible to the
        # structural scan but VISIBLE on screen). When the card holds >=2 visible candidate options,
        # this is a multi-option choice, NOT a lone consent checkbox: SEE the options, VLM-pick the one
        # meaning ctx.value, and click it BY COORDINATE. Only here (DOM read empty) — a standard radio
        # group never reaches this. If the VLM finds nothing, fall through to the heuristics below.
        cands = _visual_choice_candidates(state, ctx)
        if len(cands) >= 2 and await _visual_commit(session, ctx, cands):
            return await _s_verify(session, ctx)
        # single standalone checkbox (consent / yes-no) — toggle on an affirmative value. Only when
        # there was NOT a multi-option visual group above (a lone consent box has 0-1 candidate).
        if brain._norm_lower(ctx.value) in brain._BOOL_VALUES and brain._norm_lower(ctx.value) not in {
            "no",
            "false",
            "n",
            "decline",
        }:
            await act.click_node(session, ctx.node)
            return await _s_verify(session, ctx)
        return await _s_other_guard(session, ctx)
    texts = [t for t, _ in group]
    chosen = _identity_pick(ctx.value, texts)
    if not chosen:
        chosen = await brain.pick_option(ctx.value, texts, llm=ctx.llm, label=ctx.label)
    node = dict(group).get(chosen) if chosen else None
    if not chosen or node is None:
        # The DOM group was found but the text-pick produced no usable option (Lever's styled-div
        # radios often read back as bare markers / mismatched labels, so pick_option can't match the
        # value against them even though the choices ARE on screen). Before falling to the Other-guard
        # — which would ESCALATE a sensitive Yes/No card we CAN actually answer — try the VISUAL path:
        # mark the visible option elements and VLM-pick the one meaning ctx.value, then click BY
        # COORDINATE. Same generic fallback the empty-group branch uses, applied to the no-match case.
        cands = _visual_choice_candidates(state, ctx)
        if len(cands) >= 2 and await _visual_commit(session, ctx, cands):
            return await _s_verify(session, ctx)
        return await _s_other_guard(session, ctx)
    ctx.committed_text = chosen
    # Point ctx.node at the CHOSEN option control (it was the representative until now for a grouped
    # bind) so the verify oracle reads back the control we actually committed, not the group's first
    # option — the DOM read-back then checks the real selected radio/checkbox.
    ctx.node = node
    await act.click_node(session, node)  # TRUSTED click on the visible proxy / input
    _texts_lower = {t.strip().lower() for t in texts}
    if _reveal_pending_text(ctx.value, chosen) or (
        ctx.value.strip().lower() not in _texts_lower and any(_is_other_escape(t) for t in texts)
    ):
        return await _s_cascade(session, ctx)  # commit revealed a specify input / value needs self-describe
    return await _s_verify(session, ctx)


async def _s_multi_checkbox(session: Any, ctx: Ctx, state: perc.OAState | None = None) -> Outcome:
    """Check EVERY box in a native checkbox GROUP whose label matches a token of a multi-value (a
    multi_select like Skills/Languages = 'Python, Go'). The single-pick _s_choice commits only the head
    token; this loops the proven per-value commit (label-anchor first — self-verifies .checked — then the
    group scan) once per value. Checkboxes are independent, so each check persists. DONE when every token
    committed (each already self-verified); partial falls to _s_verify; none falls back to single-pick."""
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_MULTI_CHECKBOX")
    parts = [p.strip() for p in ctx.value.replace(";", ",").split(",") if p.strip()]
    container = ctx.card if ctx.card is not None else ctx.node
    committed: list[str] = []
    for part in parts[:MULTI_CAP]:
        got = ""
        with contextlib.suppress(Exception):
            _pg = await session.must_get_current_page()
            _btn, _ = await cdpa.cdp_choose_button_by_label(_pg, ctx.label, part)
            got = _btn
        if not got and container is not None:
            got = await cdpa.cdp_choose_option(
                session, container, part, group_name=getattr(ctx.field_obj, "name", "") or ""
            )
        if got:
            committed.append(part)
            ctx.trace.append(f"multi-cb+{part[:14]}")
    if committed:
        ctx.committed_text = ", ".join(committed)
        if len(committed) == len(parts):
            return DONE  # every token self-verified (.checked / _STILL_CHECKED) — honest DONE
        return await _s_verify(session, ctx)
    return await _s_choice(session, ctx, state)  # mis-detected multi -> single pick


def _read_choice_group(state: perc.OAState, ctx: Ctx) -> list[tuple[str, Any]]:
    """The radio/checkbox options that belong to THIS question's group, scoped to its card.

    Structure-agnostic: a control whose intrinsic kind matches the trigger. When the field was bound
    via the grouped-widget tier (``ctx.card`` set), we scope the scan to that CARD's subtree so a
    multi-question page picks the RIGHT options for THIS question — the prior whole-page scan grouped
    by intrinsic-kind across ALL questions and so could pull another question's Yes/No into this group.
    Falls back to the whole page when no card is known (a single standalone toggle). Each option's
    label is its own visible text (a radio's name IS its option, e.g. 'Yes'). Returns [(option, node)].
    """
    # The option kind we want to gather. Prefer the trigger node's OWN intrinsic kind (a real
    # <input type=radio>); but on a custom card where the located representative does NOT expose a
    # standard radio/checkbox type/role, fall back to the adapter's authoritative ctx.kind hint
    # ('radio'->INTRINSIC_RADIO, 'checkbox'->INTRINSIC_CHECKBOX) so we still scope to the matching
    # option controls. This is what makes a live Lever Yes/No card (styled inputs) read its options.
    want_kind = classify_intrinsic(ctx.node) or _kind_to_intrinsic(ctx.kind)
    # Scope to the card subtree (grouped bind) — reuse perception's own structural walk — else page.
    pool = perc._controls_in(ctx.card) if ctx.card is not None else list(state.selector_map.values())
    out: list[tuple[str, Any]] = []
    for node in pool:
        if not perc.node_is_visible(node):
            continue
        # Match the option control by its OWN intrinsic kind; if the page's option inputs (like the
        # trigger) are styled customs with no standard type, accept any fillable control in the card
        # when we have a kind hint and no node here self-identifies — the card scoping is the guard.
        nk = classify_intrinsic(node)
        if want_kind and nk and nk != want_kind:
            continue
        if not want_kind and nk not in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
            continue
        label = perc.node_label_text(node)
        if label and label.strip():
            out.append((label.strip(), node))
    return out


def _visual_choice_candidates(state: perc.OAState, ctx: Ctx) -> list[Any]:
    """The VISIBLE clickable option elements for the visual fallback when ``_read_choice_group`` was
    empty (custom styled-div radios). Scoped GEOMETRICALLY to ctx.card's box when bound (so the marks
    are THIS question's options, not the whole page), else the whole page. Drawn from browser-use's own
    indexed-interactive set (``selector_map`` — which DOES include the clickable styled option rows on
    a Lever card), filtered to visible + boxed. Generic: pure geometry + visibility, no fillable gate
    (the styled proxies are NOT standard radios) and no per-ATS hook — the set-of-marks then numbers
    them for the VLM to pick by value. Excludes the trigger node itself (it is not an option)."""
    card_box = perc.node_rect(ctx.card) if ctx.card is not None else None
    trigger_bnid = getattr(ctx.node, "backend_node_id", None)
    out: list[Any] = []
    seen: set[int] = set()
    for node in state.selector_map.values():
        bnid = getattr(node, "backend_node_id", None)
        if bnid is None or bnid in seen or bnid == trigger_bnid:
            continue
        if not perc.node_is_visible(node):
            continue
        center = perc.node_center(node)
        if center is None:  # no on-screen box -> nothing to mark/click
            continue
        if card_box is not None and not _center_in_box(center, card_box):
            continue  # scope to the bound card so a multi-question page marks THIS question's options
        seen.add(bnid)
        out.append(node)
    return out


def _center_in_box(center: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    """Is the point ``center`` (cx, cy) inside the document-space box (x, y, w, h)? A small margin
    tolerates the option-row box extending a hair past the card heading wrapper."""
    cx, cy = center
    bx, by, bw, bh = box
    return (bx - 8) <= cx <= (bx + bw + 8) and (by - 8) <= cy <= (by + bh + 8)


def _kind_to_intrinsic(kind: str) -> str:
    """Map the adapter's CHOICE kind tag to the INTRINSIC_* the choice-group reader scopes on, so a
    custom-styled radio/checkbox card (no standard type on the node) still gathers the right options."""
    k = (kind or "").strip().lower()
    if k == "radio" or k.endswith("_radio"):
        return "INTRINSIC_RADIO"
    if k == "checkbox" or k.endswith("_checkbox"):
        return "INTRINSIC_CHECKBOX"
    return ""


# ---- S_NATIVE (native <select>) ----
async def _s_native(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_NATIVE")
    options = await act.read_options(session, ctx.node)  # in-DOM <option> texts, free
    if not options:
        ctx.trace.append("native-no-options")
        return ESCALATE if ctx.required else SKIP
    # DETERMINISTIC identity first (palantir mega/64: a thousands-long school list overwhelmed
    # the LLM pick, which returned nothing for value 'Other' although 'Other (School Not
    # Listed)' sat in the list). Normalized equality, else a WORD-BOUNDARY prefix (>=4 chars,
    # next char non-alphanumeric — 'no' must never match 'North Dakota'); shortest match wins.
    _nv = " ".join(ctx.value.split()).lower()

    def _no(s: str) -> str:
        return " ".join(str(s).split()).lower()

    # NATIVE <select multiple>: a comma/semicolon value is a LIST — resolve EACH token to its option
    # (fuzzy per value: 'USA'->'United States') and select them all in one commit. cdp_select_multiple
    # returns -1 when the element is NOT multiple, so a single-select whose value legitimately holds a
    # comma ('San Francisco, CA') falls straight through to the single-pick below. Unharmed either way.
    if "," in ctx.value or ";" in ctx.value:
        _parts = [p.strip() for p in ctx.value.replace(";", ",").split(",") if p.strip()]
        _picked: list[str] = []
        for _part in _parts[:MULTI_CAP]:
            _mh = [o for o in options if _no(o) == _no(_part)]
            _ch = min(_mh, key=len) if _mh else await brain.pick_option(_part, options, llm=ctx.llm, label=ctx.label)
            if _ch and _ch not in _picked:
                _picked.append(_ch)
        if _picked:
            _cnt = await cdpa.cdp_select_multiple(session, ctx.node, _picked)
            if _cnt and _cnt > 0:
                ctx.committed_text = ", ".join(_picked)
                ctx.trace.append(f"native-multi:{_cnt}")
                return await _s_verify(session, ctx)

    _hits = [o for o in options if _no(o) == _nv]
    # DATE COMPONENT for a Month or Year <select> (a 'Graduation Date' split across two selects): an ISO
    # value has no whole-string option, so match by its month-NAME or year component instead.
    if not _hits:
        _dm = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", ctx.value.strip())
        if _dm:
            for _cand in (_MONTHS[int(_dm.group(2)) - 1], _dm.group(1), str(int(_dm.group(2)))):
                _dh = [o for o in options if _no(o) == _no(_cand)]
                if _dh:
                    ctx.committed_text = min(_dh, key=len)
                    ctx.trace.append(f"date-select:{_cand}")
                    ok = await act.select_option(session, ctx.node, ctx.committed_text)
                    if ok:
                        return await _s_verify(session, ctx)
                    break
    if not _hits and len(_nv) >= 4:
        _hits = [
            o for o in options
            if _no(o).startswith(_nv) and (len(_no(o)) == len(_nv) or not _no(o)[len(_nv)].isalnum())
        ]
    if _hits:
        chosen = min(_hits, key=len)
        ctx.trace.append("native-identity-match")
    else:
        chosen = await brain.pick_option(ctx.value, options, llm=ctx.llm, label=ctx.label)
    if not chosen:
        return await _s_other_guard(session, ctx)
    ok = await act.select_option(session, ctx.node, chosen)
    if not ok:
        ctx.trace.append("native-select-refused")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = chosen
    return await _s_verify(session, ctx)


_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _reformat_date(value: str, label: str) -> str:
    """Re-serialize an ISO ``yyyy-mm-dd`` value to the FORMAT the field asks for (read from the label —
    'Start Date (MM/DD/YYYY)', 'DOB (DD/MM/YYYY)', 'Graduation (Month YYYY)'). Typing the raw ISO into a
    Cleave MM/DD mask fed '2027-03-15' -> '20/27/0315' garbage; a 'Month YYYY' text stayed ISO. Defaults
    to KEEP the value unchanged when no format hint is present (native input[type=date] wants ISO, and an
    unhinted field is left alone — zero live regression). Only ISO inputs are touched."""
    import re as _re

    m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(value).strip())
    if not m:
        return value
    y, mo, d = m.groups()
    hint = " ".join(str(label or "").upper().split())
    if "DD/MM" in hint or "DD-MM" in hint or "DD.MM" in hint:
        return f"{d}/{mo}/{y}"
    if "MONTH" in hint and ("YYYY" in hint or "YEAR" in hint):
        return f"{_MONTHS[int(mo) - 1]} {y}"
    if "MM/YYYY" in hint or "MM-YYYY" in hint or "MM.YYYY" in hint:
        return f"{mo}/{y}"  # month + year, no day (license/expiry masks)
    if "YYYY/MM" in hint or "YYYY-MM" in hint:
        return f"{y}/{mo}"
    if "MM/DD" in hint or "MM-DD" in hint or "MM.DD" in hint:
        return f"{mo}/{d}/{y}"
    return value  # no hint -> unchanged (native date accepts ISO; don't touch live)


def _to_iso_date(value: str) -> str:
    """Coerce a date string to ISO ``yyyy-mm-dd`` for a native ``input[type=date]`` (its .value is
    ALWAYS ISO — a US/other string assigned to .value silently BLANKS the field). Accepts ISO and
    numeric MM/DD/YYYY or DD/MM/YYYY (disambiguated: a first component >12 is the day). Returns '' if
    unparseable (caller keeps the reformatted value — no worse than today)."""
    import re as _re

    s = str(value or "").strip()
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = _re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", s)
    if not m:
        return ""
    a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
    mo, d = (b, a) if a > 12 else (a, b)  # first>12 -> DD/MM; else MM/DD (US)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y}-{mo:02d}-{d:02d}"


# ---- S_DATE ----
async def _s_date(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_DATE")
    # PRESENT/CURRENT sentinel: an End Date of 'Present' is expressed by TICKING the sibling
    # 'I currently work here' checkbox (which disables the date), not by typing a date. Fires ONLY on the
    # sentinel value, so a real date types normally; no sibling checkbox -> fall through and type.
    if _is_present_value(ctx.value):
        cb = await _find_sibling_checkbox(session, ctx)
        if cb is not None and await act.click_node(session, cb):
            ctx.node = cb
            ctx.committed_text = ctx.value
            ctx.trace.append("present-toggle-check")
            return DONE  # native checkbox toggle is deterministic; verify would re-click and untick it
    # REFORMAT an ISO value to the field's requested format (from the label) BEFORE typing — a masked
    # MM/DD/YYYY / DD/MM/YYYY input mangles raw ISO digits, and a 'Month YYYY' text needs the month name.
    _v = _reformat_date(ctx.value, ctx.label)
    if _v != ctx.value:
        ctx.trace.append(f"date-reformat:{_v}")
    # NATIVE input[type=date]: .value must be ISO yyyy-mm-dd. Assigning US/other format via the
    # native setter (act.type_text's date path) blanks the field. Coerce to ISO for this node only;
    # masked/text 'Month YYYY' inputs keep the reformatted display string above.
    if _node_tag(ctx.node) == "input" and _node_attr(ctx.node, "type") == "date":
        _iso = _to_iso_date(ctx.value)
        if _iso:
            _v = _iso
            ctx.trace.append(f"date-iso:{_v}")
    # Never delta-probed. Type the value into the located control; a segmented control accepts
    # digit-by-digit via the same trusted type. (Segment-aware digit building is the engine's
    # _date job for Workday; here the located node is the date control and trusted-type lands it.)
    ok = await act.type_text(session, ctx.node, _v, clear=True)
    if not ok:
        ctx.trace.append("date-type-refused")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = _v
    return await _s_verify(session, ctx)


# ---- S3_OPEN (click & watch for a field-scoped delta) ----
async def _s3_open(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S3_OPEN")
    # DIAGNOSTIC (gitlab mega/33: aria/react-select rungs never fired, straight to click+delta ->
    # escalate; cannot tell gate-miss from hidden-<select> node without the node identity):
    with contextlib.suppress(Exception):
        ctx.trace.append(
            f"s3-node:{_node_tag(ctx.node)}/{_node_role(ctx.node) or '-'}"
            f"/combo={_is_plain_text_editable_or_combo(ctx.node)}"
        )
    # PROVEN native-<select> commit FIRST (ats_lever._select_native, generalised): browser-use's
    # select_option no-ops on Lever's React selects; setting selectedIndex by option text + firing change
    # is the deterministic path. Scan the card container for a <select> and commit it directly.
    container = ctx.card if ctx.card is not None else ctx.node
    if container is not None:
        sel = await cdpa.cdp_select_in_container(session, container, ctx.value)
        if sel:
            ctx.committed_text = sel
            ctx.trace.append(f"select-dom-direct:{sel[:20]}")
            return DONE
    # INLINE CHOICE PILLS / native (styled) radios mis-routed to S3 instead of S_CHOICE (ashby Yes/No:
    # authorized-to-work / relatives / 18+ / visa — they open no menu, so the react-select rungs below
    # read 0 owned options and escalate). Commit STRUCTURALLY via cdp_choose_option: it scopes to the
    # group by its `name` IDENTITY (discovery's structural handle, document-wide, immune to a mis-located
    # container) or scans the located card, handles button-pills AND native radios, matches the OPTION's
    # own text (short 'Yes'/'No'), and self-verifies real checked-state. NO question-label string re-scan
    # (that was the brittle 40-char match). Returns "" when nothing structural matched -> a real dropdown
    # falls through to react-select untouched.
    # Includes MULTI: Ashby renders Yes/No as an _option_ pill group with a text input, and the classifier
    # mislabels it 'multi_select' — so gating on BOOLEAN/CLOSED_LIST alone skipped it (live: nature=MULTI,
    # kind=multi_select, card present). cdp_choose_option self-verifies structurally: a single value ('Yes')
    # matches one option and commits; a REAL multi's comma-joined value matches no single option -> returns
    # "" and falls through to the multi loop. So widening to MULTI cannot break a genuine multi-select.
    if ctx.nature in ("BOOLEAN", "CLOSED_LIST", "MULTI"):
        _container = ctx.card if ctx.card is not None else ctx.node
        _gname = str(getattr(getattr(ctx, "field_obj", None), "name", "") or "")
        if _container is not None:
            with contextlib.suppress(Exception):
                _opt = await cdpa.cdp_choose_option(session, _container, ctx.value, group_name=_gname)
                if _opt and await _chosen_plausible(ctx, _opt):
                    ctx.committed_text = _opt
                    ctx.trace.append(f"s3-choice-struct:{_opt[:20]}")
                    if _reveal_pending_text(ctx.value, _opt):
                        return await _s_cascade(session, ctx)
                    return DONE
    # First try the cheap inspectable read (native/ARIA/custom dropdown_options).
    inspect = await act.read_options(session, ctx.node)
    if inspect:
        ctx.trace.append(f"read_options:{len(inspect)}")
        # VIRTUALIZED click-only combobox: read_options only sees the MOUNTED window (react-window
        # ~10 of N rows). If the wanted value isn't in it, scroll-materialize the portaled listbox by
        # exact value until it mounts. Gated to a click-only combobox whose window LACKS the value, so
        # a normal in-window option still commits via the fast path below (zero hot-path change).
        if _is_click_only_combobox(ctx.node):
            _nv = _norm_opt(ctx.value)

            def _otxt(o: Any) -> str:
                return o if isinstance(o, str) else (o.get("text") or o.get("label") or o.get("value") or "")

            if not any(_norm_opt(_otxt(o)) == _nv or (_nv and _nv in _norm_opt(_otxt(o))) for o in inspect):
                # EXACT/identity match ONLY (pick=None) — this fires when the value is missing from the
                # mounted window, which is EITHER virtualized (value is a real option, scroll finds it)
                # OR an Other-specify field (value is NOT any option). An LLM pick here would grab the
                # 'Other (please specify)' escape and commit it, skipping the S_CASCADE reveal. Exact-
                # only commits only true virtualized hits; Other-specify finds nothing -> falls through
                # to _commit_from_options, which routes the escape option into the reveal cascade.
                _g = await cdpa.cdp_pick_aria_option(session, ctx.node, ctx.value, pick=None,
                                                     node_id=str(getattr(getattr(ctx, "field_obj", None), "name", "") or ""))
                if _g and await _chosen_plausible(ctx, _g):
                    ctx.committed_text = _g
                    ctx.trace.append(f"virtualized-pick:{_g[:20]}")
                    return await _s_verify(session, ctx)
        return await _commit_from_options(session, ctx, inspect, nodes=None)
    # ARIA-DIRECT for a react-select / listbox combobox BEFORE the delta read: the input's
    # aria-owns/aria-controls names its portal listbox, so we read ONLY that listbox's [role=option]
    # and click the match — SCOPED exactly like the proven ats_lever handler scopes by field name,
    # instead of the page-wide delta that swept in 'Toggle flyout' / neighbor questions and cycled
    # robinhood's demographic selects to their 28s deadline. Deterministic, no garbage, fast.
    # Gate on the CLASSIFIED kind (SELECT) + a text-editable input, NOT on aria-owns being present
    # pre-open — react-select exposes aria-owns/aria-controls only once the menu is OPEN, and
    # cdp_choose_aria_option SELF-OPENS then follows them. Gating on the closed-state attribute is
    # why the first cut never fired (aria-direct hits = 0). Any combobox-kind input tries it.
    # BOOLEAN/CLOSED_LIST widening (watershed mega/59): an ashby Yes/No dropdown classifies as
    # nature BOOLEAN with a text input — the SELECT-only gate skipped every direct rung and the
    # field died in blind click+delta (visual clicked self x3 -> commit-cap). A combo-shaped
    # node with an options-bearing NATURE is a dropdown regardless of the kind label.
    if (
        normalize_kind(ctx.kind) == "SELECT" or ctx.nature in ("BOOLEAN", "CLOSED_LIST")
    ) and _is_plain_text_editable_or_combo(ctx.node):
        # CDP-CORE FIRST (react-select): the NAME-based open(focus+ArrowDown)+aria-scoped read+
        # trusted rect click is PROVEN reliable on this widget (twilio question_66968126: a fresh
        # core.read_options returns the 4 real options and choose_option commits), whereas the
        # node-based filter rung below (cdp_choose_react_select) THRASHES the menu into a
        # collapsing state (244 stale page opts -> None -> escalate) and only THEN falls to this
        # same core path on a corrupted menu. Run it before anything touches the menu. Falls
        # through untouched when core can't scope (returns []), so no regression on other widgets.
        # the react-select's ACTIONABLE id is on the combobox INPUT node (greenhouse: id=question_N),
        # NOT the field's descriptive name (a sibling text/hidden input 'what_is_the_source_of…' whose
        # 'open' renders no menu -> read_options=0). Prefer the node's raw DOM id, fall back to name.
        _node_id = (getattr(ctx.node, "attributes", None) or {}).get("id") or ""
        _cands = [c for c in (_node_id, str(getattr(ctx.field_obj, "name", "") or "")) if c]
        for _core_name in _cands:
            with contextlib.suppress(Exception):
                import oa_cdp_core as _core

                _opts = await _core.read_options(session, _core_name)
                if _opts:
                    _chosen = _identity_pick(ctx.value, _opts) or await brain.pick_option(
                        ctx.value, _opts, llm=ctx.llm, label=ctx.label)
                    if _chosen and await _chosen_plausible(ctx, _chosen):
                        _got = await _core.choose_option(session, _core_name, _chosen)
                        if _got:
                            ctx.committed_text = _got
                            ctx.trace.append(f"rs-core-first:{_got[:20]}")
                            return await _s_verify(session, ctx)
                    break  # found the menu on this id; don't re-open on the sibling name
        with contextlib.suppress(Exception):
            got = await cdpa.cdp_choose_aria_option(session, ctx.node, ctx.value)
            # PLAUSIBILITY at the direct rungs too (1password mega3/2: the react-select
            # page-wide option fallback read a FOREIGN menu and committed 'not listed' for
            # 'San Francisco…' — the veto was wired into the list/visual paths only).
            if got and not await _chosen_plausible(ctx, got):
                ctx.trace.append(f"aria-implausible-reject:{got[:18]}")
                got = ""
            if got:
                ctx.committed_text = got
                ctx.trace.append(f"aria-direct:{got[:20]}")
                return await _s_verify(session, ctx)
        # react-select (no aria-owns): mousedown-open + read class-based options + click match.
        # This is the robinhood/greenhouse root — a plain click never opens the menu.
        with contextlib.suppress(Exception):
            async def _pick(v: str, opts: list[str]) -> str | None:
                return await brain.pick_option(v, opts, llm=ctx.llm, label=ctx.label)

            got = await cdpa.cdp_choose_react_select(session, ctx.node, ctx.value, pick=_pick)
            if got and not await _chosen_plausible(ctx, got):
                ctx.trace.append(f"rs-implausible-reject:{got[:18]}")
                got = ""
            if got:
                ctx.committed_text = got
                ctx.trace.append(f"react-select-direct:{got[:20]}")
                # MULTI: react-select isMulti commits ONE value here then stops (playground
                # react_select_multi painted 'React' only). Loop the remaining tokens like the sibling
                # react-select paths (below + S4) already do — _s_multi_loop types each next value into
                # the same input to add its pill.
                if ctx.nature == "MULTI":
                    return await _s_multi_loop(session, ctx)
                return await _s_verify(session, ctx)
    # Else physically open and watch the delta.
    before = await perc.get_state(session)
    await act.click_node(session, ctx.node)
    cluster = await _settle(session, before, _SETTLE_STATIC_S)
    texts = _option_texts(cluster)
    if not texts:
        # NON-EDITABLE COMBOBOX (MUI Select div): the TRUSTED click above opened its React menu (a
        # synthetic dispatch could not, so the aria-direct rung missed), but the listbox portals to
        # <body> outside this field's subtree so the field-scoped delta is empty. Pick from the OPEN
        # portaled listbox by aria-ownership WITHOUT re-opening (a re-open toggles MUI shut). Never
        # type-to-filter a click-only combobox — a keystroke lands in a div and filters nothing.
        if _is_click_only_combobox(ctx.node):
            async def _pick_pl(v: str, opts: list[str]) -> str | None:
                c = _identity_pick(v, opts)
                return c or await brain.pick_option(v, opts, llm=ctx.llm, label=ctx.label)

            got = await cdpa.cdp_pick_aria_option(session, ctx.node, ctx.value, pick=_pick_pl,
                                                  node_id=str(getattr(getattr(ctx, "field_obj", None), "name", "") or ""))
            if got and await _chosen_plausible(ctx, got):
                ctx.committed_text = got
                ctx.trace.append(f"portaled-pick:{got[:20]}")
                return await _s_verify(session, ctx)
        # CARD-COMMIT FIX: a CUSTOM dropdown (Lever single_select) renders its option list only AFTER
        # a keystroke filters it — the bare click mounts no delta. When the adapter told us this is a
        # SELECT (ctx.kind), TYPE the value to filter, settle, and re-read the delta BEFORE falling to
        # the typeahead search. The control must be text-editable to accept a filter keystroke.
        # (same BOOLEAN/CLOSED_LIST widening as the aria rung — watershed mega/59)
        if (
            normalize_kind(ctx.kind) == "SELECT" or ctx.nature in ("BOOLEAN", "CLOSED_LIST")
        ) and _is_plain_text_editable_or_combo(ctx.node):
            if await _probe_would_clobber(session, ctx):
                if _occupied_is_own_field(ctx):
                    ctx.trace.append("occupied-own-field->override")
                else:
                    alt = await _rebind_empty_in_card(session, ctx)
                    if alt is None:
                        ctx.trace.append("occupied-foreign-input->escalate")
                        return await _s_other_guard(session, ctx)
                    ctx.node = alt
                    ctx.trace.append("occupied->rebound-empty-in-card")
            ctx.trace.append("select-type-to-filter")
            for probe in _filter_probes(ctx.value):
                before2 = await perc.get_state(session)
                if not await act.type_text(session, ctx.node, probe, clear=True):
                    break
                cluster = await _settle(session, before2, _SETTLE_SEARCH_S)
                texts = _option_texts(cluster)
                ctx.trace.append(f"select-filter '{probe}' -> {len(texts)} opts:{[t[:16] for t in texts[:3]]}")
                if texts:
                    break
        if not texts:
            # VISUAL FALLBACK: the custom dropdown is OPEN (we clicked + filtered) but its option list
            # rendered into a portal the DOM delta missed — yet the options are VISIBLE. SEE them: read
            # the fresh state, mark the newly-rendered visible option elements, VLM-pick the one meaning
            # ctx.value, click it BY COORDINATE. Only here (every DOM read came back empty).
            cands = await _visual_dropdown_candidates(session, ctx, before)
            if cands and await _visual_commit(session, ctx, cands):
                if ctx.nature == "MULTI":
                    return await _s_multi_loop(session, ctx)
                return await _s_cascade(session, ctx)
            ctx.trace.append("no-delta->search")
            return await _s4_search(session, ctx)  # DISAMBIGUATE — never assume text
    ctx.trace.append(f"delta-cluster:{len(texts)}")
    return await _commit_from_options(session, ctx, texts, nodes=cluster)


async def _visual_dropdown_candidates(session: Any, ctx: Ctx, before: perc.OAState) -> list[Any]:
    """The VISIBLE option elements an OPEN custom dropdown rendered into a portal the delta missed.

    The delta read found nothing, but the menu IS on screen. Re-read the state and return the visible
    elements that are NEW vs ``before`` (the portal-rendered option cells) — generic, by appearance,
    no per-ATS selector. Falls back to the page's newly-visible boxed elements when the delta-by-id is
    empty (the portal may reuse ids). These are then marked + picked-by-value in ``_visual_commit``."""
    after = await perc.get_state(session)
    new = perc.delta(before, after)
    cands = [d.node for d in new if d.node is not None and perc.node_center(d.node) is not None]
    if cands:
        return cands
    # No id-delta (portal reused ids) — mark every visible boxed control on the page; the VLM picks
    # the option whose text means ctx.value, so extra candidates are harmless (it returns -1 on none).
    return [n for n in after.selector_map.values() if perc.node_is_visible(n) and perc.node_center(n) is not None]


async def _commit_from_options(session: Any, ctx: Ctx, texts: list[str], nodes: list[perc.DeltaNode] | None) -> Outcome:
    """S_CLOSED_LIST: pick + commit from a read option set (with optional cluster nodes).
    Long list -> bounded scroll-reread on no-match. Records committed_text."""
    if len(texts) >= _LIST_LONG:
        ctx.trace.append("long-list")
    chosen = _identity_pick(ctx.value, texts)
    if chosen:
        ctx.trace.append("identity-pick")
    else:
        chosen = await brain.pick_option(ctx.value, texts, llm=ctx.llm, label=ctx.label)
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
    if chosen and not await _chosen_plausible(ctx, chosen):
        ctx.trace.append(f"pick-implausible-reject:{chosen[:20]}")
        chosen = None
    if not chosen:
        # FILTER RETRY (stripe mega3/6 Country: the delta's 5 'options' were chrome and the
        # veto rightly killed the pick — but the node IS an editable combobox and a keystroke
        # filter surfaces the REAL options; airbnb Gender same with a 1-junk delta. The old
        # flow went straight to OTHER_GUARD and the field died unfilled.)
        if not ctx.filter_tried and _is_plain_text_editable_or_combo(ctx.node):
            ctx.filter_tried = True
            fresh: list = []
            ftexts: list = []
            for probe in _filter_probes(ctx.value):
                before_f = await perc.get_state(session)
                if not await act.type_text(session, ctx.node, probe, clear=True):
                    break
                fresh = await _settle(session, before_f, _SETTLE_SEARCH_S)
                ftexts = _option_texts(fresh)
                ctx.trace.append(f"pick-fail->filter '{probe}' -> {len(ftexts)} opts:{[t[:14] for t in ftexts[:3]]}")
                if ftexts:
                    break
            if True:
                if ftexts:
                    # deterministic identity first (stripe mega3/6 'country where you reside':
                    # the filter narrowed to ONE option and the LLM pick still returned nothing)
                    fchosen = _identity_pick(ctx.value, ftexts)
                    if not fchosen:
                        fchosen = await brain.pick_option(ctx.value, ftexts, llm=ctx.llm, label=ctx.label)
                    if fchosen and await _chosen_plausible(ctx, fchosen):
                        fnode = _node_for_option(fresh, fchosen)
                        if fnode is not None and await act.click_node(session, fnode):
                            ctx.committed_text = fchosen
                            ctx.trace.append("filter-retry-commit")
                            return await _s_verify(session, ctx)
        # REACT-SELECT DIRECT on the pick-fail path (twilio mega4/32: every delta/filter read
        # returned junk while the tenant's DEFAULT answer sat committed on screen — the RS rung
        # reads its OWN menu via instance-id/aria-controls and can commit here, but it was only
        # wired into the native-fail path).
        if _node_role(ctx.node) == "combobox" or _node_attr(ctx.node, "aria-autocomplete") not in ("", "none"):
            # CDP-CORE FIRST (owner mandate + live-proven on this exact widget): resolve the
            # field FRESH by discovery identity, keyboard-open, aria-scoped read, trusted rect
            # click. Replaces the flaky node-based cdp_choose_react_select (stripe mega4/5 rerun
            # 'rs-direct no options: aria=True pageOpts=244' — the node path couldn't scope the
            # menu; core.choose_option committed 'NO' on this same widget in a live session).
            _core_name = str(getattr(ctx.field_obj, "name", "") or "")
            if _core_name:
                with contextlib.suppress(Exception):
                    import oa_cdp_core as core

                    _opts = await core.read_options(session, _core_name)
                    if _opts:
                        _chosen = _identity_pick(ctx.value, _opts) or await brain.pick_option(
                            ctx.value, _opts, llm=ctx.llm, label=ctx.label)
                        if _chosen and await _chosen_plausible(ctx, _chosen):
                            _got = await core.choose_option(session, _core_name, _chosen)
                            if _got:
                                ctx.committed_text = _got
                                ctx.trace.append(f"rs-core-commit:{_got[:20]}")
                                return await _s_verify(session, ctx)
            with contextlib.suppress(Exception):

                async def _rs_pick(v: str, opts: list) -> str | None:
                    c = _identity_pick(v, opts)
                    if not c:
                        c = await brain.pick_option(v, opts, llm=ctx.llm, label=ctx.label)
                    return c if (c and await _chosen_plausible(ctx, c)) else None

                got = await cdpa.cdp_choose_react_select(session, ctx.node, ctx.value, pick=_rs_pick)
                if got:
                    ctx.committed_text = got
                    ctx.trace.append(f"rs-direct-commit:{got[:20]}")
                    return await _s_verify(session, ctx)
        # MISCLASSIFIED-SELECT recovery on THIS abandon path too (reddit mega3/61-62,
        # greenhouse API: 'How did you hear' is input_text — the VLM called it a dropdown,
        # the filter typed into a menu-less box, and this branch escalated without ever
        # trying the S4-exhaustion text recovery). NOT when the field has real discovered
        # options — an unmatchable value on a genuine closed list is a hallucination that must
        # escalate, not get typed as free text (airwallex mega4/49 relatives multi_select).
        _has_real_options = bool(getattr(getattr(ctx, "field_obj", None), "options", None)) or ctx.nature in ("MULTI", "BOOLEAN")
        if _is_plain_text_editable(ctx.node) and not _has_real_options:
            ctx.trace.append("misclassified-select->text")
            return await _s_text(session, ctx)
        return await _s_other_guard(session, ctx)

    ctx.committed_text = chosen
    committed = False
    # react-select / greenhouse: type the chosen text to FILTER the menu to it, then click the
    # highlighted option by TRUSTED COORDINATE. NOT Enter — a stray Enter when the menu isn't open
    # propagates to the form and RELOADS/submits it (robinhood form went blank). Clicking the
    # option cell alone is what react-select ignores; filter-then-coordinate-click is reliable AND
    # never touches the submit path. Structural combobox only, never a plain editable.
    _is_rs_combo = _node_role(ctx.node) == "combobox" or _node_attr(ctx.node, "aria-autocomplete") not in ("", "none")
    if _is_rs_combo and _is_plain_text_editable_or_combo(ctx.node):
        with contextlib.suppress(Exception):
            before_f = await perc.get_state(session)
            if await act.type_text(session, ctx.node, chosen, clear=True):
                fresh = await _settle(session, before_f, _SETTLE_SEARCH_S)
                ftexts = _option_texts(fresh)
                # the FULL menu already decided `chosen` — the filtered menu can EXCLUDE that
                # exact row and leave only lookalikes (airbnb mega4/14: full-string filter
                # showed only 'South San Francisco…' and the LLM re-pick downgraded the correct
                # 'San Francisco…' into it). Exact/identity match only here; a semantic re-pick
                # of an already-made decision is never allowed.
                fchosen = next((t for t in ftexts if t.strip().lower() == chosen.strip().lower()), None)
                if not fchosen:
                    fchosen = _identity_pick(ctx.value, ftexts)
                fnode = _node_for_option(fresh, fchosen) if fchosen else None
                if fnode is not None and await act.click_node(session, fnode):
                    ctx.committed_text = fchosen
                    ctx.trace.append("filter-coord-commit")
                    if await _s_verify(session, ctx) == DONE:
                        return DONE
                    ctx.trace.append("filter-commit-unverified")
                elif ftexts:
                    ctx.trace.append(f"filter-lost-chosen '{chosen[:20]}' -> fall through")
    if nodes is not None:
        node = _node_for_option(nodes, chosen)
        if node is not None:
            committed = await act.click_node(session, node)  # TRUSTED click the option cell
    if not committed:
        # inspectable widget OR Enter-on-highlight commit path
        committed = await act.select_option(session, ctx.node, chosen)
    if not committed and nodes is None:
        # READ-BUT-CANT-SELECT (workable react-select rating: read_options found the hidden
        # <option> texts so we took the native path, but select_option no-ops on the custom
        # widget). PHYSICALLY open it and click the rendered option DETERMINISTICALLY: the
        # delta node whose text IS the chosen option (_node_for_option, the same matcher the
        # delta path uses). The VLM mark-pick is LAST resort only — opening the menu shifts
        # everything below it, the shifted question blocks enter the candidate set, and the
        # VLM mis-picked a neighbouring question label (wk9: committed_text = the Java
        # question). Generic, no per-ATS branch.
        with contextlib.suppress(Exception):
            # ARIA-DIRECT first, BEFORE any state read: the combobox's aria-owns/aria-controls
            # names its listbox; the helper self-opens and clicks the [role=option] by text.
            # Verified live (workable): the listbox UNMOUNTS when closed and a state read between
            # open and pick blur-closes it — which is also why the delta is blind here and the
            # VLM mark-pick saw only shifted question blocks (wk9 committed a neighbour's label).
            got = await cdpa.cdp_choose_aria_option(session, ctx.node, chosen)
            if got:
                ctx.committed_text = got
                ctx.trace.append(f"aria-option-click:{got[:20]}")
                committed = True
            if not committed:
                before = await perc.get_state(session)
                await act.click_node(session, ctx.node)
                cluster = await _settle(session, before, _SETTLE_STATIC_S)
                onode = _node_for_option(cluster, chosen)
                if onode is not None and await act.click_node(session, onode):
                    ctx.trace.append("read-native-fail->delta-option-click")
                    committed = True
            if not committed:
                cands = await _visual_dropdown_candidates(session, ctx, before)
                if cands and await _visual_commit(session, ctx, cands):
                    ctx.trace.append("read-native-fail->visual-commit")
                    if ctx.nature == "MULTI":
                        return await _s_multi_loop(session, ctx)
                    return await _s_cascade(session, ctx)
    if not committed:
        ctx.trace.append("commit-failed")
        return ESCALATE if ctx.required else SKIP

    if ctx.nature == "MULTI":
        return await _s_multi_loop(session, ctx)
    return await _s_cascade(session, ctx)


# ---- TYPEAHEAD / GEOCOMPLETE blur-safety (commit a real option ROW, never bless the typed echo) ----
def _is_typeahead_editable(ctx: Ctx) -> bool:
    """An editable control that filters an OPTION MENU as you type — a react-select / geocomplete /
    location typeahead — whose typed query is a candidate for the wipe-on-blur echo false-green.
    Structural gate: keystroke-fillable-or-combobox AND (combobox-shaped OR an option-bearing
    nature). Includes comboboxes (``_is_plain_text_editable`` vetoes those); excludes a native
    <select> (not keystroke-fillable) and a plain FREE_TEXT/DATE box (no discard-on-blur menu)."""
    if not _is_plain_text_editable_or_combo(ctx.node):
        return False
    comboish = _node_role(ctx.node) == "combobox" or _node_attr(ctx.node, "aria-autocomplete") not in ("", "none")
    return comboish or ctx.nature in ("SEARCH", "CLOSED_LIST", "BOOLEAN", "MULTI")


async def _typeahead_committed_survives_blur(session: Any, ctx: Ctx) -> bool:
    """A typeahead's DOM read-back reads the TYPED text pre-blur, so a value that was only typed
    (no option row clicked) verifies CORRECT — then the portal DISCARDS it on blur (geocomplete
    wipe-on-blur / menu-left-open) and the painted field is BLANK. Confirm the value SURVIVES a real
    blur. The control's OWN .value discriminates: EMPTY means the value is either a committed
    selection rendered elsewhere (react-select single-value -> survives) or nothing at all (an S4
    echo that already cleared -> not committed) — the container-scan read tells them apart. NON-EMPTY
    means the value sits in the input, so BLUR it, settle, re-read: gone/placeholder == an
    uncommitted echo -> False; still there == a real commit -> True."""
    from oa_dom_value import read_dom_value

    raw = await cdpa.cdp_raw_value(session, ctx.node)
    if not raw:
        # value not in the input: a real react-select selection renders a single-value/chip the
        # container scan finds (survives); a cleared/never-committed typeahead reads back "".
        return bool((await read_dom_value(session, ctx.node) or "").strip())
    await cdpa.cdp_blur(session, ctx.node)
    await asyncio.sleep(_SETTLE_STATIC_S)
    # read the RAW .value post-blur (NOT read_dom_value: its combobox menu-open guard returns "" for
    # a still-open aria-expanded input even when .value survived — a real geocomplete keeps .value
    # after an option-row commit). Empty == the typed query was discarded -> uncommitted echo.
    cur = (await cdpa.cdp_raw_value(session, ctx.node) or "").strip()
    return bool(cur)  # empty == the typed query was discarded on blur (uncommitted echo)


async def _recommit_typeahead_row(session: Any, ctx: Ctx) -> Outcome:
    """Re-open the menu (type the value) and CLICK a real option ROW meaning ctx.value — a FRESH node
    by COORDINATE. Uses the SAME option-scoped extractors the search loop uses (``_option_texts``
    drops the card/label/chrome the raw candidate scan swept in), then blur-confirms the commit. No
    row means the value, or the click does not survive blur -> escalate; never bless the typed echo."""
    ctx.trace.append("S_TYPEAHEAD_RECOMMIT")
    probe = _city_prefix(ctx.value) or ctx.value
    before = await perc.get_state(session)
    typed = await act.type_text(session, ctx.node, probe, clear=True)
    if not typed:  # a just-blurred typeahead may need a re-focus click before it accepts keystrokes
        await act.click_node(session, ctx.node)
        typed = await act.type_text(session, ctx.node, probe, clear=True)
    if not typed:
        ctx.trace.append("typeahead-recommit:type-failed")
        return ESCALATE if ctx.required else SKIP
    # POLL past the async geocode latency: the option rows land ~1.2-1.4s after the keystroke, but
    # _settle's coarse reads-cap (3 x 0.30s = 0.9s) returns before then. Re-read the delta until the
    # option rows mount (or a 3s ceiling) — a geocode is known-async, unlike a click-open menu.
    cluster: list[perc.DeltaNode] = []
    texts: list[str] = []
    _deadline = time.monotonic() + 3.0
    while time.monotonic() < _deadline:
        await asyncio.sleep(_POLL_S)
        after = await perc.get_state(session, previous=before)
        cluster = perc.delta(before, after)
        texts = _option_texts(cluster)
        if texts:
            break
    ctx.trace.append(f"typeahead-recommit:{len(texts)} opts:{[t[:16] for t in texts[:3]]}")
    if not texts:
        return ESCALATE if ctx.required else SKIP
    chosen = _identity_pick(ctx.value, texts) or await brain.pick_option(ctx.value, texts, llm=ctx.llm, label=ctx.label)
    if not chosen or not await _chosen_plausible(ctx, chosen):
        ctx.trace.append("typeahead-recommit:no-matching-row")
        return ESCALATE if ctx.required else SKIP
    node = _node_for_option(cluster, chosen)
    if node is None or perc.node_center(node) is None:
        ctx.trace.append("typeahead-recommit:no-node")
        return ESCALATE if ctx.required else SKIP
    ok = await act.click_node_center(session, node) or await act.click_node(session, node)
    if not ok:
        ctx.trace.append("typeahead-recommit:click-failed")
        return ESCALATE if ctx.required else SKIP
    ctx.committed_text = chosen
    ctx.trace.append(f"typeahead-recommit-click:{chosen[:24]}")
    await asyncio.sleep(_SETTLE_STATIC_S)
    if await _typeahead_committed_survives_blur(session, ctx):
        ctx.trace.append("typeahead-recommit:survives-blur->DONE")
        return DONE
    ctx.trace.append("typeahead-recommit:blur-wiped->escalate")
    return ESCALATE if ctx.required else SKIP


# ---- S4_SEARCH (typeahead search-loop, §5) ----
async def _s4_search(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S4_SEARCH")
    # Precondition: the located element must be text-editable (a native <select> would have
    # been caught by intrinsic and routed to S_NATIVE).
    if await _probe_would_clobber(session, ctx):
        if _occupied_is_own_field(ctx):
            ctx.trace.append("occupied-own-field->override")
        else:
            alt = await _rebind_empty_in_card(session, ctx)
            if alt is None:
                ctx.trace.append("occupied-foreign-input->escalate")
                return ESCALATE if ctx.required else SKIP
            ctx.node = alt
            ctx.trace.append("occupied->rebound-empty-in-card")
    variants = await brain.query_variants(ctx.value, ctx.nature or "SEARCH", llm=ctx.llm)
    # CARD-COMMIT FIX (geocomplete): a react-select location typeahead returns NOTHING for the full
    # 'City, ST, USA' string — it matches on a short CITY prefix and resolves the rest async. Prepend
    # the leading comma-token (the city) as a high-priority variant so the first probe is e.g.
    # 'Detroit' not 'Detroit, MI, USA'. GENERIC: any comma-bearing SEARCH value (a location-shaped
    # field); the existing pick_option still chooses the closest full option the geocomplete returns.
    city_prefix = _city_prefix(ctx.value)
    if city_prefix and city_prefix.lower() not in {v.lower() for v in variants}:
        variants = [city_prefix, *variants]
    # REACT-SELECT PREFIX PROBES: a react-select filters its options by the typed text. When the real
    # option label is LONGER-worded than the mapped value ('None of the above' vs 'None of the above —
    # I do not have work authorization'; 'Other' vs 'Other (please specify)'), typing the full value can
    # still miss and the widget renders its "No options" message. Lead with the value's SHORT prefixes
    # (first word, first two words) so a longer option prefix-matches and actually surfaces. Cheap: these
    # are just extra early queries; pick_option still chooses the closest full option that appears.
    _w = ctx.value.strip().split()
    _pfx = []
    if len(_w) >= 2:
        _pfx.append(" ".join(_w[:2]))
    if _w and len(_w[0]) >= 3:
        _pfx.append(_w[0])
    for _p in _pfx:
        if _p.lower() not in {v.lower() for v in variants}:
            variants = [_p, *variants]
    for q in variants:
        if ctx.search_tries >= VARIANT_CAP:
            break
        if q.lower() in {x.lower() for x in ctx.queries_tried}:
            continue
        ctx.queries_tried.append(q)
        ctx.search_tries += 1
        before = await perc.get_state(session)
        # ONE call with the FULL query. The old probe-then-rest split (4 chars clear=True, then
        # the tail clear=False) corrupted every SEARCH field on the fast path: type_text's
        # cdp_set_value REPLACES the value, so the tail overwrote the probe and the field kept
        # 'ersity of California, Berkeley' — the deterministic head-loss on
        # breezy/hibob/bamboohr. One set fires one input event with the full string; debounced
        # suggestion lists filter on it the same.
        typed = await act.type_text(session, ctx.node, q, clear=True)
        if not typed:
            continue
        # a geocomplete (the city-prefix variant) resolves its suggestions async over the network —
        # give it the longer settle so the option cluster mounts before we read the delta.
        settle_s = _SETTLE_GEO_S if q == city_prefix else _SETTLE_SEARCH_S
        cluster = await _settle(session, before, settle_s)
        texts = _option_texts(cluster)
        # react-select renders a "No options" / "No results" placeholder AS an option row when the
        # filter matches nothing — drop it so a 0-match probe isn't mistaken for 1 real option.
        texts = [t for t in texts if not re.match(r"^\s*no (options?|results?|matches?)\b", t, re.I)]
        ctx.trace.append(f"search '{q}' -> {len(texts)} opts")
        if not texts:
            # VISUAL FALLBACK (geocomplete): we typed the city prefix and the suggestion list IS
            # rendered, but react-select painted it into a portal the delta never captured. SEE the
            # suggestions: mark the newly-rendered visible rows + VLM-pick the one matching ctx.value +
            # click BY COORDINATE. Only when this probe's DOM delta was empty (the options exist on
            # screen). If the VLM also finds nothing -> advance to the next variant, as before.
            cands = await _visual_dropdown_candidates(session, ctx, before)
            if cands and await _visual_commit(session, ctx, cands):
                if ctx.nature == "MULTI":
                    return await _s_multi_loop(session, ctx)
                return await _s_cascade(session, ctx)
            continue  # this variant produced nothing — advance
        chosen = await brain.pick_option(ctx.value, texts, llm=ctx.llm, label=ctx.label)
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
    # MISCLASSIFIED-SELECT RECOVERY (airwallex mega/38 notice-period / hear-about): a PLAIN
    # non-combo text input the VLM called a dropdown mounted ZERO options across the S3 filter
    # AND every S4 variant — it is a text field. Type the value and verify like one; the verify
    # oracle still guards a real-but-blind widget (wrong read-back -> revalue/escalate as usual).
    # BUT NOT when the field was DISCOVERED with real options (a genuine closed list): 0 options
    # then means the search TERM matched nothing = the mapped VALUE is a hallucination, not that
    # the widget is a text box. Typing the hallucination as free text and verifying its own .value
    # is a wrong-value false-green (airwallex mega4/49: relatives Yes/No multi_select got the mapper
    # value 'AI Policy for the Application process', matched 0 options, was typed as text and
    # verify-CORRECT'd). A real closed list with an unmatchable value must ESCALATE (fall through).
    _has_real_options = bool(getattr(getattr(ctx, "field_obj", None), "options", None)) or ctx.nature in ("MULTI", "BOOLEAN")
    if _is_plain_text_editable(ctx.node) and not _has_real_options:
        ctx.trace.append("misclassified-select->text")
        return await _s_text(session, ctx)
    # GEOCOMPLETE fill-only (the proven ats_lever._location trick): a React location autocomplete returns
    # no usable suggestion for a synthetic search, but fill-only only needs the value VISIBLY present +
    # read-back to pass — we do NOT need a geocode pick. Find the text input inside the location card and
    # SET its value via the native setter + input/change (cdp_set_text_in_container) so React keeps it.
    # ONLY for a genuine SEARCH typeahead on a plain input: a BOOLEAN/SELECT that exhausted its
    # search rungs has OPTIONS that must be SELECTED — writing the text into its input is a fake
    # commit the dom read-back then blesses (anthropic 'AI Policy' got .value='I acknowledge',
    # vision+crop both saw the dropdown still unanswered; robinhood office location same class
    # on the react-select filter box). Those now fall to search-exhausted -> escalate (HITL).
    container = ctx.card if ctx.card is not None else ctx.node
    _comboish = _node_role(ctx.node) == "combobox" or _node_attr(ctx.node, "aria-autocomplete") not in ("", "none")
    if container is not None and ctx.nature == "SEARCH" and not _comboish:
        set_val = await cdpa.cdp_set_text_in_container(session, container, ctx.value)
        if set_val:
            ctx.committed_text = ctx.value
            ctx.trace.append("geocomplete->set-value")
            return await _s_verify(session, ctx)
    ctx.trace.append("search-exhausted")
    return await _s_other_guard(session, ctx)


# ---- S_TEXT_GUARD / S_TEXT ----
async def _s_text_guard(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_TEXT_GUARD")
    # RANGE SLIDER (lydia mega/72: years-of-experience slider sat at its default 1 vs the
    # profile's 7 — type_text no-ops on a range input and every audit is value-blind here).
    # Deterministic: numeric part of the value, clamped to min/max, native setter + events.
    if _node_tag(ctx.node) == "input" and (_node_attr(ctx.node, "type") or "").lower() == "range":
        got = await cdpa.cdp_set_range(session, ctx.node, ctx.value)
        if got:
            ctx.committed_text = got
            ctx.trace.append(f"range-set:{got}")
            return await _s_verify(session, ctx)
        ctx.trace.append("range-set-failed")
        return ESCALATE if ctx.required else SKIP
    # Defend a map mis-tag: if the element is actually a combobox, route to search, never type.
    if not _is_plain_text_editable(ctx.node):
        ctx.trace.append("not-plain-text->search")
        return await _s4_search(session, ctx)
    # OCCUPIED-FOREIGN-INPUT (text lane): an UNTRUSTED locate (spatial/grouped/structure — not the
    # discovery's own dom-ref) bound to a plain input that already holds a DIFFERENT value is
    # almost certainly a NEIGHBOUR's control — typing clear=True destroys it (agility mega/24:
    # a yes/no question spatially bound to the Phone input, Phone became 'Yes'). dom-ref located
    # fields are exempt (retype/revalue of one's own field is legitimate).
    if "located:dom-ref" not in " ".join(ctx.trace) and await _probe_would_clobber(session, ctx):
        # same triple chain as S3/S4 (mega2/31: grouped bound LinkedIn Profile* to an occupied
        # box and the text lane could ONLY escalate — override/rebind were wired elsewhere).
        if _occupied_is_own_field(ctx):
            ctx.trace.append("occupied-own-field->override")
        else:
            alt = await _rebind_empty_in_card(session, ctx)
            if alt is None:
                ctx.trace.append("occupied-foreign-input->escalate")
                return ESCALATE if ctx.required else SKIP
            ctx.node = alt
            ctx.trace.append("occupied->rebound-empty-in-card")
    return await _s_text(session, ctx)


def _norm_opt(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


async def _commit_creatable(session: Any, ctx: Ctx, before: perc.OAState) -> str | None:
    """After typing into a PLAIN input, commit a creatable/typeahead combobox-in-disguise by CLICKING
    the rendered option row — the EXACT-value row, else the `Create \"<value>\"` row (value embedded
    verbatim). Returns the committed text, or None when no menu surfaced / no exact-or-Create row
    (caller keeps the plain-text commit). Deterministic (no LLM fuzzy -> never snaps 'Sr.' to
    'Senior'); CLICKS the option, never presses Enter -> safe on the hot _s_text path."""
    cluster = await _settle(session, before, _SETTLE_STATIC_S)
    texts = _option_texts(cluster)
    if not texts:
        return None
    nv = _norm_opt(ctx.value)
    if not nv:
        return None
    exact = next((t for t in texts if _norm_opt(t) == nv), None)
    create = next((t for t in texts if _norm_opt(t) != nv and nv in _norm_opt(t)), None)
    target = exact or create
    if not target:
        return None
    node = _node_for_option(cluster, target)
    if node is None:
        return None
    await act.click_node(session, node)
    return ctx.value if exact is None else target


# ---- COMPOSITE SIBLING SPLIT (one value -> two sibling controls) ----
# A single profile scalar sometimes carries TWO components that render as TWO inputs inside ONE
# field group: a phone + its extension ('(415) 555-0199 x1234' -> phone box + 'Ext.' box) or a
# salary range ('$120,000 - $150,000' -> 'Minimum' + 'Maximum'). ctx.node is the label-matched
# FIRST input; the SECOND is an empty plain-text sibling in the same container that gets NO injected
# value of its own. Gate is TIGHT: fire only when the VALUE is a composite (ext token, or a $/number
# range) AND an empty sibling input exists -- a plain single phone/text field has neither, so the hot
# path and live pages are untouched. ponytail: US national extraction (drop a leading '1' country
# code); a non-US '+CC ...' widget is the intl-tel per-char absorb path in act.type_text, not this one.
_EXT_RE = re.compile(r"\b(?:ext|extension|x)\b\.?\s*(\d{1,6})\s*$", re.I)
_MONEY_RANGE_RE = re.compile(r"^\s*(\$?\d[\d,]*)\s*[-–—]\s*(\$?\d[\d,]*)\s*$")
# street address + unit -> line1 | line2. The unit keyword must FOLLOW a space (so a street named
# 'Unit Ave' at the start doesn't split), and _split_composite_to_sibling only fires when an empty
# second input actually exists, so a single-line address (no line2) safely falls back to one fill.
_APT_RE = re.compile(r"^(.+?),?\s+((?:apt|apartment|suite|ste|unit|#|no\.|bldg|building|fl|floor|rm|room)\b.*)$", re.I)
# compensation amount + rate basis: '$104,000 per year' -> amount input + a 'Per year' <select>. The
# secondary is a rate PHRASE (normalized to 'Per <period>'); _split_composite_to_sibling routes it to a
# sibling <select> when there is no text sibling. Only fires on 'amount per|/ period', so a plain salary
# ('$104,000') has no match and stays a single fill.
_RATE_WORDS = {"hour": "Per hour", "hr": "Per hour", "week": "Per week", "wk": "Per week",
               "month": "Per month", "mo": "Per month", "year": "Per year", "yr": "Per year", "annum": "Per year"}
_RATE_RE = re.compile(r"^\s*(\$?[\d,]+(?:\.\d{1,2})?)\s*(?:/|per)\s*(hour|hr|week|wk|month|mo|year|yr|annum)\b", re.I)
# intl phone with a leading dial code -> national digits into the number input + the '+CC' into a
# SEPARATE country/flag selector (intl-tel separateDialCode). national = value minus the '+CC'.
# Only a '+CC ' prefix followed by phone-ish content; a plain national number has no match.
_PHONE_DIAL_RE = re.compile(r"^\s*(\+\d{1,3})[\s.\-]+([\d][\d\s().\-]{5,})$")


def _composite_parts(value: str) -> tuple[str, str] | None:
    """(primary -> ctx.node, secondary -> empty sibling) for a composite value, else None."""
    v = " ".join((value or "").split())
    m = _EXT_RE.search(v)
    if m:  # phone + extension: national digits -> phone box, ext digits -> ext box
        digits = re.sub(r"\D", "", v[: m.start()])
        if len(digits) >= 10:
            return (digits[-10:] if len(digits) > 10 else digits), m.group(1)
        return None
    m = _MONEY_RANGE_RE.match(v)
    if m:  # numeric/salary range: low -> min box, high -> max box
        return m.group(1), m.group(2)
    m = _APT_RE.match(v)
    if m:  # street address + unit: line1 -> line1 box, unit -> line2 box
        return m.group(1).rstrip(", ").strip(), m.group(2).strip()
    m = _RATE_RE.match(v)
    if m:  # comp amount + rate: amount -> input, 'Per <period>' -> a sibling <select>
        return m.group(1).strip(), _RATE_WORDS[m.group(2).lower()]
    m = _PHONE_DIAL_RE.match(v)
    if m:  # intl phone: national -> number input, '+CC' -> a sibling flag/country selector
        return m.group(2).strip(), m.group(1).strip()
    return None


async def _split_composite_to_sibling(session: Any, ctx: Ctx) -> Outcome | None:
    """Type the primary into ctx.node and the secondary into the empty sibling input; DONE only when
    BOTH read back non-empty, else None (caller re-types the whole value into the single control)."""
    parts = _composite_parts(ctx.value)
    if parts is None or not _is_plain_text_editable(ctx.node):
        return None
    primary, secondary = parts
    if not await act.type_text(session, ctx.node, primary, clear=True):
        return None
    from oa_dom_value import read_dom_value
    # read the primary back NOW — _find_revealed_input calls get_state (re-serialize), which stales
    # ctx.node's backend id; a read AFTER that returned "" and false-failed the whole split (address_line2
    # painted BLANK|BLANK intermittently). Capture it against the still-fresh node first.
    a = ((await read_dom_value(session, ctx.node)) or "").strip()
    sib = await _find_revealed_input(session, ctx)
    if sib is not None:
        await act.type_text(session, sib, secondary, clear=True)
        b = ((await read_dom_value(session, sib)) or "").strip()
    else:
        # no text sibling -> the secondary maps to a sibling SELECTOR. Climb from the primary input
        # (by stable id, stale-proof) to the field container and commit it there. Two selector kinds:
        #   * a '+CC' dial code -> a country/flag [role=listbox] with [data-dial] options (intl-tel):
        #     click the matching option; the widget writes the .iti__selected-dial-code.
        #   * anything else -> a native <select> (comp RATE basis): match the option by text.
        # Returns the committed label/dial; "" if there's no such selector (not a split field).
        _nid = str((getattr(ctx.node, "attributes", None) or {}).get("id") or "")
        if not _nid:
            return None
        _pg = await session.must_get_current_page()
        if secondary.startswith("+"):
            _js = ("() => { const el=document.getElementById(%s); const want=%s; if(!el||!want) return '';"
                   " let c=el; for(let i=0;i<6&&c;i++){"
                   "   const li=[...c.querySelectorAll('[data-dial]')].find(o=>(o.getAttribute('data-dial')||'').trim()===want);"
                   "   if(li){ const btn=c.querySelector('[aria-haspopup=listbox],button'); if(btn) btn.click();"
                   "     for(const e of ['mousedown','mouseup','click']) li.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true,view:window}));"
                   "     const dc=c.querySelector('.iti__selected-dial-code');"
                   "     return JSON.stringify({dc:(dc?dc.textContent:want).trim(), cid:c.id||''}); }"
                   "   c=c.parentElement; } return ''; }") % (json.dumps(_nid), json.dumps(secondary))
        else:
            _js = ("() => { const el=document.getElementById(%s); const want=(%s||'').toLowerCase();"
                   " if(!el||!want) return ''; let c=el;"
                   " for(let i=0;i<5&&c;i++){ const s=c.querySelector('select');"
                   "   if(s){ const o=[...s.options].find(o=>o.text.trim().toLowerCase()===want)"
                   "     || [...s.options].find(o=>{const t=o.text.trim().toLowerCase(); return t&&(t.includes(want)||want.includes(t));});"
                   "     if(o){ s.value=o.value; s.dispatchEvent(new Event('input',{bubbles:true}));"
                   "       s.dispatchEvent(new Event('change',{bubbles:true})); return o.text.trim(); } }"
                   "   c=c.parentElement; } return ''; }") % (json.dumps(_nid), json.dumps(secondary))
        b = ""
        with contextlib.suppress(Exception):
            _raw = await _pg.evaluate(_js)
            if secondary.startswith("+") and isinstance(_raw, str) and _raw.startswith("{"):
                _obj = json.loads(_raw)
                b = str(_obj.get("dc") or "").strip()
                # PHANTOM guard: the dial code was set on the flag selector inside this widget container.
                # Record the container id so the flag BUTTON (discovered as a separate phantom field with
                # a flag-glyph label) is skipped in S1_LOCATE instead of resetting the dial to +1.
                _cid = str(_obj.get("cid") or "")
                if _cid and ctx.committed_nodes is not None:
                    ctx.committed_nodes[_cid] = "__widget__"
            else:
                b = (_raw or "").strip()
    if not a or not b:
        return None
    ctx.committed_text = f"{a} | {b}"
    ctx.trace.append(f"composite-split:{a}|{b}")
    return DONE


async def _s_text(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_TEXT")
    _cs = await _split_composite_to_sibling(session, ctx)
    if _cs is not None:
        return _cs
    _val = ctx.value
    _ml = _node_attr(ctx.node, "maxlength")
    if _ml.isdigit() and int(_ml) > 0 and len(_val) > int(_ml):
        # A native value-setter (cdp_set_value / page.fill) BYPASSES the HTML maxlength cap, so the DOM
        # holds the full string while the browser keeps only the first maxLength chars on real keystrokes.
        # Pre-truncate so the committed value matches what a capped control actually stores.
        _val = _val[: int(_ml)]
        ctx.trace.append(f"maxlen-trunc:{_ml}")
    before = await perc.get_state(session)
    ok = await act.type_text(session, ctx.node, _val, clear=True)
    if not ok:
        ctx.trace.append("text-type-refused")
        return ESCALATE if ctx.required else SKIP
    # CREATABLE-COMBOBOX-IN-DISGUISE: a bare <input> (no role=combobox) whose typing surfaces a menu is
    # a react-select Creatable etc. — a native-setter type writes the input's .value but the widget only
    # paints its committed display when the option ROW is chosen, so painted stays BLANK (false-green on
    # the input's own .value read-back). If typing opened a menu offering the value EXACTLY or a
    # `Create \"<value>\"` row, click it verbatim (never fuzzy, never Enter). Plain inputs surface
    # nothing -> keep the plain-text commit (hot path unchanged; geocomplete stays verbatim).
    picked = await _commit_creatable(session, ctx, before)
    if picked is not None:
        ctx.trace.append("creatable-commit")
    ctx.committed_text = picked if picked is not None else _val
    return await _s_verify(session, ctx)


async def _s_listbox(session: Any, ctx: Ctx) -> Outcome:
    """ARIA role=listbox (NOT a native <select>): click each matching [role=option] cell by text.
    Handles single + aria-multiselectable (a comma/semicolon value = multiple picks). Self-verifies via
    _s_verify (real aria-selected / painted state), so an over-eager click cannot false-green."""
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_LISTBOX")
    parts = [p.strip() for p in ctx.value.replace(";", ",").split(",") if p.strip()] or [ctx.value]
    n = await cdpa.cdp_click_listbox_options(session, ctx.node, parts[:MULTI_CAP])
    if n and n > 0:
        ctx.committed_text = ", ".join(parts[:MULTI_CAP])
        ctx.trace.append(f"listbox-click:{n}")
        return await _s_verify(session, ctx)
    return await _s_native(session, ctx)  # nothing matched -> fall back (also covers a mis-detected select)


# ---- Other-specify reveal detection (structural, no fixed label strings) ----
_OTHER_ESCAPE_RE = re.compile(r"\b(other|specify|self[- ]describe|something else|prefer to (?:self[- ])?describe|not listed)\b", re.I)


def _is_other_escape(text: str) -> bool:
    """STRUCTURAL escape-option signal: the committed option is a generic 'Other / please specify /
    something else / prefer to self-describe' bucket whose purpose is to REVEAL a dependent free-text
    input. Matched by the OPTION's own words (word-boundary so 'another'/'mother' don't trip), never
    by the field label. Principle + example: 'Other (please specify)' qualifies; 'Referral' does not."""
    return bool(text and _OTHER_ESCAPE_RE.search(text))


def _reveal_residual(value: str, committed: str) -> str:
    """Sub-field text = value minus the committed option when that option is a LEADING prefix
    ('Yes, H-1B' -> 'H-1B'); otherwise the whole value ('Other (please specify)' -> full value)."""
    v = (value or "").strip()
    c = (committed or "").strip()
    if c and v.lower().startswith(c.lower()):
        rest = v[len(c):].lstrip(" ,;:-").strip()
        if rest:
            return rest
    return v


def _reveal_pending_text(value: str, committed: str) -> bool:
    """Cheap (no DOM) pre-check for routing a choice commit into S_CASCADE: the committed option is an
    'other/self-describe' escape bucket, OR it is a leading prefix of a longer value carrying detail.
    A plain closed-list commit (value == committed, no escape word) returns False -> hot path skips."""
    v = (value or "").strip()
    c = (committed or "").strip()
    if not v or v.lower() == c.lower():
        return False
    return _is_other_escape(c) or _reveal_residual(v, c) != v


_PRESENT_VALUES = {
    "present", "current", "currently", "ongoing", "now", "to present", "till date",
    "current position", "i currently work here",
}


def _is_present_value(value: str) -> bool:
    """The End-Date value is a 'currently employed' sentinel, not a date -> tick the present toggle."""
    return (value or "").strip().lower() in _PRESENT_VALUES


async def _find_revealed_input(session: Any, ctx: "Ctx") -> Any | None:
    """After an escape-option commit, find the dependent text input the choice REVEALED.

    Structural, no label strings: the 'please specify' box was display:none during S1_LOCATE so it is
    NOT in the old state -> re-perceive. Relocate the committed control by discovery identity (dom-ref)
    or, lacking one, by nearest rect (the select did not move). Then climb to the smallest ancestor
    that ALSO holds an empty, visible, plain-text input that is NOT inside the committed combobox
    itself. Containment scopes it to THIS field, so a neighbouring field's empty input is never
    grabbed; None -> caller falls through to verify unchanged."""
    from oa_dom_value import read_dom_value

    base_rect = perc.node_rect(ctx.node)
    state = await perc.get_state(session)
    ref = str(getattr(getattr(ctx, "field_obj", None), "name", "") or "")
    node = perc._locate_by_dom_ref(state, ref) if ref else None
    if node is None and base_rect is not None:
        # no usable identity: relocate the committed control geometrically (it did not move).
        bx, by = base_rect[0] + base_rect[2] / 2, base_rect[1] + base_rect[3] / 2
        best_d = 1e9
        for n in state.selector_map.values():
            r = perc.node_rect(n)
            if not r:
                continue
            d = abs((r[0] + r[2] / 2) - bx) + abs((r[1] + r[3] / 2) - by)
            if d < best_d:
                best_d, node = d, n
    if node is None:
        return None
    own = getattr(node, "backend_node_id", None)
    cur = node
    for _ in range(6):
        parent = getattr(cur, "parent_node", None)
        if parent is None:
            break
        cur = parent
        for n in perc._controls_in(cur):
            if getattr(n, "backend_node_id", None) == own:
                continue
            if perc._is_descendant(n, node):  # skip the committed combobox's OWN inner input
                continue
            # HARD veto radio/checkbox/button/non-text inputs: _is_plain_text_editable relies on the
            # perc node's type attr, which reads "" for some radios -> "" || "text" => wrongly editable.
            # Typing into the sibling RADIO set its .value=residual and fired its change (yesno: flipped
            # 'No'->'H-1B', whose change else-branch re-hid the reveal + wiped the real input). Read the
            # type straight from the node's attributes and skip anything not a free-text field.
            _ty = str((getattr(n, "attributes", None) or {}).get("type") or _node_attr(n, "type") or "").lower()
            if _ty in ("radio", "checkbox", "button", "submit", "reset", "file", "hidden", "range", "color", "image"):
                continue
            if not _is_plain_text_editable(n) or not perc.node_is_visible(n):
                continue
            existing = ""
            with contextlib.suppress(Exception):
                existing = ((await read_dom_value(session, n)) or "").strip()
            if not existing:
                return n
    return None


async def _find_sibling_checkbox(session: Any, ctx: "Ctx") -> Any | None:
    """The 'I currently work here / present' checkbox sharing a work-history row with the End Date input.
    Structural (no label words): relocate the located date control (dom-ref, else nearest rect) then climb
    to the smallest ancestor that ALSO holds a visible checkbox. None -> caller types the date normally."""
    base_rect = perc.node_rect(ctx.node)
    state = await perc.get_state(session)
    ref = str(getattr(getattr(ctx, "field_obj", None), "name", "") or "")
    node = perc._locate_by_dom_ref(state, ref) if ref else None
    if node is None and base_rect is not None:
        bx, by = base_rect[0] + base_rect[2] / 2, base_rect[1] + base_rect[3] / 2
        best_d = 1e9
        for n in state.selector_map.values():
            r = perc.node_rect(n)
            if not r:
                continue
            d = abs((r[0] + r[2] / 2) - bx) + abs((r[1] + r[3] / 2) - by)
            if d < best_d:
                best_d, node = d, n
    if node is None:
        node = ctx.node
    cur = node
    for _ in range(6):
        parent = getattr(cur, "parent_node", None)
        if parent is None:
            break
        cur = parent
        for n in perc._controls_in(cur):
            if classify_intrinsic(n) == "INTRINSIC_CHECKBOX" and perc.node_is_visible(n):
                return n
    return None


# ---- S_CASCADE (did the commit reveal a sub-field?) ----
async def _s_cascade(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_CASCADE")
    # OTHER-SPECIFY REVEAL: the commit chose an escape bucket ('Other (please specify)') because the
    # wanted value is not a listed option — choosing it unhid a dependent text input that must receive
    # ctx.value. Gate TIGHT: fire only when the committed option READS as an escape bucket AND differs
    # from the wanted value. A genuine closed-list commit sets committed_text to a real option (no
    # escape word) so this whole rung is skipped and the hot path / live pages are untouched.
    value = ctx.value.strip()
    committed = (ctx.committed_text or "").strip()
    residual = _reveal_residual(value, committed)
    # ARM A — the commit REVEALED a dependent input: an 'Other/self-describe' escape bucket, OR an
    # affirmative option carrying trailing detail ('Yes, H-1B'). Fill the newly-visible input with the
    # RESIDUAL (value minus the option) and verify against exactly that residual. Gate = residual/escape
    # signal AND the input actually being found, so a plain closed-list commit skips this (hot path).
    if value and value.lower() != committed.lower() and (_is_other_escape(committed) or residual != value):
        rin = await _find_revealed_input(session, ctx)
        if rin is not None and await act.type_text(session, rin, residual, clear=True):
            ctx.node = rin  # verify now reads the input that holds the REAL value, not the combobox
            ctx.card = None
            ctx.value = residual  # verify compares the input against exactly what we typed
            ctx.committed_text = residual
            ctx.trace.append("other-specify-fill")
            from oa_dom_value import read_dom_value as _rdv
            _rb = ((await _rdv(session, ctx.node)) or "").strip()
            if _rb and (_rb.lower() == residual.strip().lower() or residual.strip().lower() in _rb.lower()):
                ctx.trace.append("reveal-verified-dom")
                return DONE  # revealed input holds the value; NEVER vlm/revalue (revalue clobbers it)
            return await _s_verify(session, ctx)
    # ARM B — wanted value is NOT an offered option AND the on-screen radio group carries a
    # 'self-describe / other' escape option: switch the pick to that escape (which reveals its input)
    # and type the full value in. Radio/checkbox groups only (a native select's Other is Arm A);
    # gated on value != committed so an exact-match pick never re-picks. Fixes gender self-describe.
    elif (
        value
        and value.lower() != committed.lower()
        and classify_intrinsic(ctx.node) in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX")
    ):
        state = await perc.get_state(session)
        group = _read_choice_group(state, ctx)  # ponytail: whole-page scope when card is None; routing gate already confirmed this field has a self-describe escape
        if not any(t.strip().lower() == value.lower() for t, _ in group):
            esc = next(
                ((t, n) for t, n in group if _is_other_escape(t) and t.strip().lower() != committed.lower()),
                None,
            )
            if esc is not None:
                await act.click_node(session, esc[1])
                ctx.node = esc[1]
                ctx.committed_text = esc[0]
                ctx.trace.append(f"cascade-escape:{esc[0][:20]}")
                rin = await _find_revealed_input(session, ctx)
                if rin is not None and await act.type_text(session, rin, value, clear=True):
                    ctx.node = rin
                    ctx.card = None
                    ctx.committed_text = value
                    ctx.trace.append("other-specify-fill")
                    from oa_dom_value import read_dom_value as _rdv
                    _rb = ((await _rdv(session, ctx.node)) or "").strip()
                    if _rb and (_rb.lower() == value.strip().lower() or value.strip().lower() in _rb.lower()):
                        ctx.trace.append("reveal-verified-dom")
                        return DONE  # revealed input holds value; NEVER vlm/revalue (revalue clobbers it)
                    return await _s_verify(session, ctx)
    # Bounded; the single-page proof does not derive child values (no profile here), so cascade
    # only guards against runaway recursion and hands off to verify. A real sub-field would be
    # discovered by the runner's next field; here we verify the parent commit.
    return await _s_verify(session, ctx)


# ---- S_MULTI_LOOP (multi-value chips: Skills / Languages) ----
async def _s_multi_loop(session: Any, ctx: Ctx, first_committed: bool = True) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_MULTI_LOOP")
    parts = [p.strip() for p in ctx.value.replace(";", ",").split(",") if p.strip()]
    if first_committed:
        ctx.multi_done += 1  # the caller already committed the first value (react-select-direct etc.)

    # BARE CHIP BOX (react-tag-input / Ashby skills): a MULTI on a PLAIN text input (S2 routes only a
    # non-combobox here) mints a chip per token via type+Enter with NO option menu. Fire every token in
    # ONE CDP call (single resolve): a per-token get_state re-resolve invalidates the input's backend
    # node id ('Node does not belong to the document') so only the first chip landed, and the native-
    # setter type BLURS the field so a SendKeys Enter missed the chip handler / hit the <form> (painted
    # BLANK, final_url gained '?'). Enter is dispatched UNTRUSTED (JS KeyboardEvent) so it fires the
    # widget's keydown listener but can NEVER submit the form; gated to a plain input so it never runs
    # on a typeahead/plain single field.
    if _is_plain_text_editable(ctx.node):
        want = parts[ctx.multi_done : MULTI_CAP]
        n = await cdpa.cdp_type_enter_chips(session, ctx.node, want)
        ctx.multi_done += n
        ctx.committed_text = ", ".join(parts[:MULTI_CAP])
        ctx.trace.append(f"multi-chips:{n}/{len(want)}")
        return await _s_verify(session, ctx)

    async def _pick(v: str, opts: list[str]) -> str | None:
        return await brain.pick_option(v, opts, llm=ctx.llm, label=ctx.label)

    for part in parts[ctx.multi_done :]:
        if ctx.multi_done >= MULTI_CAP:
            break
        if not ctx.guard():
            break
        # REACT-SELECT isMulti: reuse the SAME primitive that committed token #1 — it opens the still-open
        # menu scoped to THIS control and clicks the matching option, adding a pill. The bespoke
        # type->delta->Enter below never surfaced a fresh delta (closeMenuOnSelect=false) so only the 1st
        # pill landed. Returns "" for a plain chip input, so those fall through to type+Enter.
        with contextlib.suppress(Exception):
            got = await cdpa.cdp_choose_react_select(session, ctx.node, part, pick=_pick)
            if got:
                ctx.multi_done += 1
                ctx.trace.append(f"multi-rs+ {part}")
                continue
        before = await perc.get_state(session)
        typed = await act.type_text(session, ctx.node, part, clear=True)
        if not typed:
            continue
        cluster = await _settle(session, before, _SETTLE_SEARCH_S)
        texts = _option_texts(cluster)
        chosen = await brain.pick_option(part, texts, llm=ctx.llm, label=ctx.label) if texts else None
        node = _node_for_option(cluster, chosen) if chosen else None
        if node is not None:
            await act.click_node(session, node)
        else:
            # tag/chip input (no option menu appears): Enter commits the typed value as a chip.
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

    # TYPEAHEAD BLUR-SAFETY (BEFORE the DOM read-back, which reads the pre-blur TYPED echo): a
    # geocomplete/location typeahead whose value was only typed (never committed via an option-row
    # click) reads .value = the typed text, so the read-back says CORRECT — then the portal DISCARDS
    # it on blur and the painted field is BLANK / menu-left-open (artie Location, gridware Current
    # location). Confirm the value SURVIVES a real blur first; if it wipes, the typed text was never
    # committed -> re-commit by CLICKING a real option ROW (fresh, by coordinate). Fires once per
    # field (the recommit returns terminally; it never re-enters _s_verify).
    if _is_typeahead_editable(ctx) and not ctx.typeahead_checked:
        ctx.typeahead_checked = True
        if not await _typeahead_committed_survives_blur(session, ctx):
            ctx.trace.append("typeahead-echo-not-committed")
            return await _recommit_typeahead_row(session, ctx)
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


# ---- OBSERVE-BEFORE-REPAIR (铁律 2: repair decisions come from FRESH PIXELS, never stale DOM) ----
async def _observed_already_correct(session: Any, ctx: Ctx, *, stage: str) -> bool:
    """One fresh, uncached VLM look at the field BEFORE any destructive repair. The EMPTY/WRONG
    verdict that routed us here can be a stale/desynced DOM read (grouped-locate, next-tick React
    state) — repairing off it re-clicks toggles OFF or wipes a correct value. If the pixels already
    show the wanted value, the repair is aborted and the field is DONE.

    SCOPE + BUDGET: fires ONLY when the routing verdict came from a DOM read (``verify-src:dom``) —
    that is the stale/desynced case observe-before-repair exists for; if a VLM aid already looked
    and reported EMPTY/WRONG, re-looking is pointless (same model, same pixels) and would blow the
    per-field VLM cap (the stubborn-field selftest) + starve the set-of-marks visual COMMIT one rung
    later (the toggle_switch_radix regression). Capped at ONE look per field (``observe_used``); does
    NOT charge FIELD_VLM_CAP. False on any failure (repair proceeds exactly as before)."""
    want = (ctx.committed_text or ctx.value or "").strip()
    # ONE free look per field, and never once the per-field VLM budget is spent (a VLM-exhausted
    # stubborn field must stop looking — the budget selftest). Free = does not increment vlm_used,
    # so it never starves the set-of-marks visual COMMIT that runs on the same budget one rung later.
    if not want or ctx.observe_used or ctx.vlm_used >= FIELD_VLM_CAP:
        return False
    srcs = [t for t in ctx.trace if t.startswith("verify-src:")]
    if not srcs or srcs[-1] != "verify-src:dom":
        return False  # the verdict was VLM-sourced (or none): the VLM already looked — don't re-look
    ctx.observe_used = True
    with contextlib.suppress(Exception):
        # the field was just interacted with, so it is in the viewport the screenshot captures
        import vision_verify as _vv2

        verdict = await _vv2.visual_check(
            session, ctx.label, want=want, key=f"{ctx.label}:{stage}", use_cache=False
        )
        if _vv2._matches(verdict):
            ctx.trace.append(f"{stage}:fresh-observe-correct->DONE")
            return True
    return False


# ---- S_RECOMMIT (EMPTY: click never registered) ----
async def _s_recommit(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    # fresh pixels FIRST: a false-EMPTY (desynced DOM) must not trigger a duplicate commit
    # (a re-click on an already-selected pill/checkbox TOGGLES IT OFF — destructive).
    if await _observed_already_correct(session, ctx, stage=f"recommit#{ctx.commit_tries + 1}"):
        return DONE
    if ctx.commit_tries >= COMMIT_CAP:
        ctx.trace.append("commit-cap")
        # NOTE: a render-truth "accept if painted" here is UNSAFE — rendered_present matched a Yes/No
        # button-group's ever-present option LABEL ('No') and greened an UNSELECTED field (live
        # false-green, apolink 'worked on satellite missions'). Honest escalation > false-green; the
        # real react-select cases are still caught safely by the end-of-run ledger reconciliation.
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
    # fresh pixels FIRST: a stale WRONG verdict must not clear a value that is correct on screen.
    if await _observed_already_correct(session, ctx, stage=f"revalue#{ctx.revalue_tries + 1}"):
        return DONE
    if ctx.revalue_tries >= REVALUE_CAP:
        ctx.trace.append("revalue-cap")
        return ESCALATE if ctx.required else SKIP
    ctx.revalue_tries += 1
    ctx.trace.append(f"S_REVALUE#{ctx.revalue_tries}")
    # clear the wrong value, then re-search with the next UNUSED variant (dedup via queries_tried).
    await act.type_text(session, ctx.node, "", clear=True)
    return await _s4_search(session, ctx)


# ---- S_OTHER_GUARD / S_OTHER ----
async def _clear_probe_residue(session: Any, ctx: Ctx) -> None:
    """On abandonment, erase OUR OWN probe text left in the box (affirm mega3/44: the
    previously-employed probe 'I have' stayed behind in the mislocated empty Name
    Pronunciation input; samsara/32 'Acknow' same). Deterministic: only clears when the
    current value EXACTLY matches something we typed this field (probe prefix or query)."""
    with contextlib.suppress(Exception):
        from oa_dom_value import read_dom_value

        cur = ((await read_dom_value(session, ctx.node)) or "").strip()
        if not cur:
            return
        probes = {q.strip() for q in (ctx.queries_tried or [])}
        v = (ctx.value or "").strip()
        if v:
            probes.add(v[: min(len(v), 6)])
        if cur in probes and cur.lower() != v.lower():
            await act.type_text(session, ctx.node, "", clear=True)
            ctx.trace.append("probe-residue-cleared")


async def _s_other_guard(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_OTHER_GUARD")
    await _clear_probe_residue(session, ctx)
    if not ctx.required:
        return SKIP
    # Demographic / screening / legal labels: escalate ONLY when we have no answer to give.
    # When the mapper supplied a value — a disclosed profile fact or a user-sanctioned default
    # (veteran/disability -> No; government-official / worked-for-X -> No) — filling it is the
    # POINT (user judged the screenshots: 'veteran/disability by default 都是没有… gender 没有写'
    # was a MISS, not caution). No value -> honest ESCALATE (HITL).
    if _is_sensitive(ctx.label) and not (ctx.value or "").strip():
        ctx.trace.append("sensitive-no-value->ESCALATE")
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
    # CDP-CORE react-select recovery (last chance before HITL): a BOOLEAN/combobox that fell through
    # the filter/search/visual rungs (airtable 'require visa sponsorship?' Yes/No react-select:
    # select-filter 'No' -> 0 opts, visual self-label-reject, search 0 opts) can still be filled by
    # the PROVEN name-based open(scroll+ArrowDown)+aria-scoped read+trusted click that fixed twilio
    # source-of-right — this S3/S4 path never wired it in. Resolve by the react-select input's DOM id
    # (fall back to the field name). The S_VERIFY + render-verify/wipe gates re-check, so no false-green.
    if ctx.nature in ("BOOLEAN", "CLOSED_LIST") or _node_role(ctx.node) == "combobox" or _node_attr(ctx.node, "aria-autocomplete") not in ("", "none"):
        _cn = (getattr(ctx.node, "attributes", None) or {}).get("id") or str(getattr(ctx.field_obj, "name", "") or "")
        if _cn:
            with contextlib.suppress(Exception):
                import oa_cdp_core as _core

                _opts = await _core.read_options(session, _cn)
                if _opts:
                    _ch = _identity_pick(ctx.value, _opts) or await brain.pick_option(
                        ctx.value, _opts, llm=ctx.llm, label=ctx.label)
                    if _ch and await _chosen_plausible(ctx, _ch):
                        _got = await _core.choose_option(session, _cn, _ch)
                        if _got:
                            ctx.committed_text = _got
                            ctx.trace.append(f"s-other-rs-core:{_got[:18]}")
                            return await _s_verify(session, ctx)
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
    chk(
        "native date -> ISO",
        _to_iso_date("03/15/2027") == "2027-03-15"
        and _to_iso_date("2027-03-15") == "2027-03-15"
        and _to_iso_date("15/03/2027") == "2027-03-15"
        and _to_iso_date("March 2027") == "",
    )
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
    # committed by EITHER mechanism: a react-select combo now commits via keyboard (type+Enter,
    # the canonical interaction) instead of an option-cell click that react-select ignores.
    chk("closed-list committed text",
        "Bachelor's Degree" in (fs.last_click_text or "") or "Bachelor's Degree" in (fs.last_type_text or ""),
        (fs.last_click_text, fs.last_type_text))

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

    # ===== THE CARD-COMMIT FIX (the LAST gap, proven from runs/final3/lever.json) =====
    # The live Lever screening CARDS are LOCATED (located:grouped) but the representative control does
    # NOT self-identify as an intrinsic radio/checkbox/select (a styled custom proxy), so
    # classify_intrinsic == "" and the label-LLM mis-derives BOOLEAN/MULTI/SEARCH -> the wrong path ->
    # 0 opts -> ESCALATE. The ADAPTER already parsed each card's REAL type (radio|checkbox|
    # single_select|textarea); that ``kind`` hint now routes correctly BEFORE the label-LLM guess.
    from oa_observe_act_fakes import (
        make_custom_choice_card,
        make_custom_select_card,
        make_single_input_card,
    )

    # (25) RADIO CARD via KIND HINT — the live mis-route case. A 'Yes/No' radio whose option controls
    #      have NO standard type/role (classify_intrinsic == "") + a label that would classify BOOLEAN.
    #      The kind='radio' hint MUST route to S_CHOICE (click the visible 'Yes'), NEVER S3_OPEN/S4_SEARCH.
    auth_card = make_custom_choice_card(
        "Are you legally authorized to work in the country for which you are applying?",
        ["Yes", "No"],
        base_bnid=1000,
    )
    fs25 = FakeSession(controls=auth_card, dom_values={auth_card[0].backend_node_id: "Yes"})
    fd25 = {
        "label": "Are you legally authorized to work in the country for which you are applying?",
        "value": "Yes",
        "required": True,
        "kind": "radio",  # the adapter's authoritative control type
        "llm": fake_llm,
    }
    out25 = await observe_act(fs25, fd25)
    tr25 = fd25.get("_trace") or []
    chk(
        "RADIO card kind-hint -> S_CHOICE clicks 'Yes' (NOT S4_SEARCH) -> DONE",
        out25 == DONE
        and "kind-hint:radio->CHOICE" in tr25
        and "S_CHOICE" in tr25
        and "S4_SEARCH" not in tr25
        and "S3_OPEN" not in tr25
        and fd25.get("_committed") == "Yes",
        (out25, fd25.get("_committed"), tr25),
    )

    # (26) CHECKBOX CARD via KIND HINT (Language Skills) — custom checkboxes, value 'English'. The
    #      kind='checkbox' hint routes to S_CHOICE and CLICKS the 'English' box (NOT type-search 0 opts).
    lang_card = make_custom_choice_card(
        "Language Skill(s) (Check all that apply)",
        ["English", "Spanish", "French"],
        base_bnid=1020,
    )
    english = lang_card[0]
    fs26 = FakeSession(controls=lang_card, dom_values={english.backend_node_id: "English"})
    fd26 = {
        "label": "Language Skill(s) (Check all that apply)",
        "value": "English",
        "required": True,
        "kind": "checkbox",
        "cardinality": "many",
        "llm": fake_llm,
    }
    out26 = await observe_act(fs26, fd26)
    tr26 = fd26.get("_trace") or []
    chk(
        "CHECKBOX card kind-hint -> S_CHOICE clicks 'English' (NOT S4_SEARCH) -> DONE",
        out26 == DONE
        and "kind-hint:checkbox->CHOICE" in tr26
        and "S_CHOICE" in tr26
        and "S4_SEARCH" not in tr26
        and fd26.get("_committed") == "English",
        (out26, fd26.get("_committed"), tr26),
    )

    # (27) SINGLE_SELECT CARD via KIND HINT — a CUSTOM combobox dropdown (NOT a native <select>):
    #      read_options is empty, a bare click mounts NO delta, options render only after a filter
    #      keystroke. kind='single_select' routes to S3_OPEN, which TYPES the value to filter and reads
    #      the delta, then commits 'LinkedIn'. Proves the open+type-to-filter+read path for Lever selects.
    hear = make_custom_select_card("Please tell us how you heard about this opportunity.", bnid=1040)
    fs27 = FakeSession(
        controls=[hear],
        on_type_delta={hear.backend_node_id: [("LinkedIn", (100, 320)), ("Referral", (100, 350))]},
        read_options_map={},  # custom widget exposes NO inspectable options
        dom_values={hear.backend_node_id: "LinkedIn"},
        verdict='{"filled": true, "matches": true}',
    )
    fd27 = {
        "label": "Please tell us how you heard about this opportunity.",
        "value": "LinkedIn",
        "required": True,
        "kind": "single_select",
        "llm": fake_llm,
    }
    out27 = await observe_act(fs27, fd27)
    tr27 = fd27.get("_trace") or []
    chk(
        "SINGLE_SELECT card kind-hint -> S3_OPEN type-to-filter reads + commits 'LinkedIn' -> DONE",
        out27 == DONE
        and "kind-hint:single_select->SELECT" in tr27
        and "S3_OPEN" in tr27
        and "select-type-to-filter" in tr27
        and fd27.get("_committed") == "LinkedIn",
        (out27, fd27.get("_committed"), tr27),
    )

    # (28) TEXTAREA CARD via KIND HINT — kind='textarea' routes to FREE_TEXT (the text path) even if
    #      the label wording might tempt a different classify. Proves textarea kind -> S_TEXT.
    why = make_single_input_card("Why do you want to work here?", bnid=1060, tag="textarea")
    fs28 = FakeSession(controls=[why], dom_values={why.backend_node_id: "Because I admire the mission."})
    fd28 = {
        "label": "Why do you want to work here?",
        "value": "Because I admire the mission.",
        "required": True,
        "kind": "textarea",
        "llm": fake_llm,
    }
    out28 = await observe_act(fs28, fd28)
    tr28 = fd28.get("_trace") or []
    chk(
        "TEXTAREA card kind-hint -> S_TEXT types value -> DONE",
        out28 == DONE and "kind-hint:textarea->TEXTAREA" in tr28 and "S_TEXT" in tr28,
        (out28, fs28.last_type_text, tr28),
    )

    # (29) LOCATION GEOCOMPLETE — the react-select location typeahead returns 0 opts for the full
    #      'City, ST, USA', but matches on a short CITY PREFIX. S4_SEARCH must try the leading
    #      comma-token ('Detroit') FIRST and commit the resolved suggestion. NO kind hint (a plain
    #      text location field) — proves the generic city-prefix variant + longer geo settle.
    loc = _mk(tag="input", role="combobox", attrs={"aria-autocomplete": "list"}, ax_name="Current location")
    fs29 = FakeSession(
        controls=[loc],
        # the geocomplete only yields options for the city-prefix 'Detroit', NOT the full string.
        on_type_delta={loc.backend_node_id: [("Detroit, MI, USA", (100, 250))]},
        dom_values={loc.backend_node_id: "Detroit, MI, USA"},
        verdict='{"filled": true, "matches": true}',
    )
    fd29 = {"label": "Current location", "value": "Detroit, MI, USA", "required": True, "llm": fake_llm}
    out29 = await observe_act(fs29, fd29)
    tr29 = fd29.get("_trace") or []
    chk(
        "LOCATION geocomplete -> city-prefix 'Detroit' variant tried first -> DONE",
        out29 == DONE and any(t.startswith("search 'Detroit'") for t in tr29) and fd29.get("_committed"),
        (out29, fd29.get("_committed"), tr29),
    )

    # (30) NO-REGRESS: a kind='text' (or no kind) field is UNTOUCHED by the hint route — it still
    #      classifies via the label path (FREE_TEXT here). Proves the hint never hijacks plain text.
    name30 = _mk(tag="input", typ="text", ax_name="First Name")
    fs30 = FakeSession(controls=[name30], dom_values={name30.backend_node_id: "Diego"})
    fd30 = {"label": "First Name", "value": "Diego", "required": True, "kind": "text", "llm": fake_llm}
    out30 = await observe_act(fs30, fd30)
    tr30 = fd30.get("_trace") or []
    chk(
        "kind='text' does NOT trigger a kind-hint route (plain text path intact)",
        out30 == DONE and not any(t.startswith("kind-hint:") for t in tr30) and "S_TEXT" in tr30,
        (out30, tr30),
    )

    # ===== THE VISUAL COMMIT FALLBACK (the LAST gap, proven from runs/cards/lever.json) =====
    # The custom Lever screening widgets do NOT expose options to standard DOM reads: radio cards are
    # styled DIVs (not <input type=radio>) so _read_choice_group is EMPTY; a custom single_select and a
    # geocomplete render their options into a portal the delta misses. The values are KNOWN and the
    # options are VISIBLE — so we SEE them: screenshot + set-of-marks over the visible option elements +
    # VLM pick-by-VALUE + click BY COORDINATE (cdp_click_xy). A STRICT FALLBACK (only when the DOM read
    # came back empty) so it never touches a standard widget.
    from oa_observe_act_fakes import (
        install_marks_vlm,
        make_visual_radio_card,
        restore_vlm,
    )

    # (31) VISUAL RADIO COMMIT — the exact lever.json case. A custom Yes/No card whose options are
    #      styled DIVs: located:grouped -> S_CHOICE -> _read_choice_group EMPTY (choice-no-group) ->
    #      the VISUAL path marks the option divs, the VLM returns the 'Yes' div's bnid, and the engine
    #      CLICKS IT BY COORDINATE (cdp_click_xy). DOM read-back ('Yes') then verifies CORRECT.
    _vv._VLM_CALLS["n"] = 0
    reset_page_vlm_backstop()
    vcard = make_visual_radio_card("How should we contact you about this role?", ["Yes", "No"], base_bnid=1100)
    yes_div = vcard[1]  # controls = [trigger, yes_div, no_div]
    fs31 = FakeSession(controls=vcard, dom_values={yes_div.backend_node_id: "Yes"})
    orig_vlm31 = install_marks_vlm(f'{{"mark": {yes_div.backend_node_id}}}')
    try:
        fd31 = {
            "label": "How should we contact you about this role?",
            "value": "Yes",
            "required": True,
            "kind": "radio",
            "llm": fake_llm,
        }
        out31 = await observe_act(fs31, fd31)
    finally:
        restore_vlm(orig_vlm31)
    tr31 = fd31.get("_trace") or []
    chk(
        "VISUAL radio: empty group -> set-of-marks 'Yes' -> combined cdp_click -> DONE",
        out31 == DONE and "choice-no-group" in tr31 and any(t.startswith("visual-choice+cdp_click") for t in tr31),
        (out31, tr31),
    )

    # (32) VISUAL RADIO on a SENSITIVE label — the live authorize/sponsorship/AI-consent case. The
    #      visual path must fire BEFORE the sensitive Other-guard (which previously ESCALATED these),
    #      so a sensitive Yes/No still commits visually when its value is known + visible.
    _vv._VLM_CALLS["n"] = 0
    scard = make_visual_radio_card(
        "Are you legally authorized to work in the country for which you are applying?",
        ["Yes", "No"],
        base_bnid=1140,
    )
    yes_div2 = scard[1]
    fs32 = FakeSession(controls=scard, dom_values={yes_div2.backend_node_id: "Yes"})
    orig_vlm32 = install_marks_vlm(f'{{"mark": {yes_div2.backend_node_id}}}')
    try:
        fd32 = {
            "label": "Are you legally authorized to work in the country for which you are applying?",
            "value": "Yes",
            "required": True,
            "kind": "radio",
            "llm": fake_llm,
        }
        out32 = await observe_act(fs32, fd32)
    finally:
        restore_vlm(orig_vlm32)
    tr32 = fd32.get("_trace") or []
    chk(
        "VISUAL radio fires BEFORE sensitive-guard -> DONE (no ESCALATE)",
        out32 == DONE
        and any(t.startswith("visual-choice+cdp_click") for t in tr32)
        and "sensitive->ESCALATE" not in tr32,
        (out32, tr32),
    )

    # (33) VISUAL RADIO no-match -> the VLM returns -1 -> visual-choice:none -> the EXISTING guard
    #      still runs (a sensitive label then ESCALATES, never a silent Other). Proves the fallback is
    #      additive: when the VLM also finds nothing, behaviour is exactly as before the fix.
    _vv._VLM_CALLS["n"] = 0
    scard2 = make_visual_radio_card(
        "Will you now or in the future require sponsorship for employment visa status?",
        ["Yes", "No"],
        base_bnid=1180,
    )
    fs33 = FakeSession(controls=scard2)
    orig_vlm33 = install_marks_vlm('{"mark": -1}')  # the VLM finds no matching option
    try:
        fd33 = {
            "label": "Will you now or in the future require sponsorship for employment visa status?",
            "value": "No",
            "required": True,
            "kind": "radio",
            "llm": fake_llm,
        }
        out33 = await observe_act(fs33, fd33)
    finally:
        restore_vlm(orig_vlm33)
    tr33 = fd33.get("_trace") or []
    chk(
        # policy change (user-sanctioned): a sensitive field WITH a mapped value now TRIES to
        # answer (S_OTHER) instead of short-circuiting at the sensitive guard; the guard only
        # fires when no value exists. Outcome here is still ESCALATE (S_OTHER found no escape).
        "VISUAL radio no-match -> S_OTHER attempted -> ESCALATE (additive)",
        out33 == ESCALATE and "visual-choice:none" in tr33
        and ("sensitive-no-value->ESCALATE" in tr33 or "no-escape->ESCALATE" in tr33),
        (out33, tr33),
    )

    # (34) NO-REGRESS: a STANDARD radio card (intrinsic <input type=radio>, options readable by the DOM
    #      group read) NEVER reaches the visual path — _read_choice_group succeeds, no screenshot/VLM
    #      marks spend. Proves the visual fallback is a strict fallback gated on an EMPTY DOM read.
    _vv._VLM_CALLS["n"] = 0
    from oa_observe_act_fakes import make_choice_card

    std = make_choice_card("Do you have a valid work permit?", ["Yes", "No"], base_bnid=1220, kind="radio")
    fs34 = FakeSession(controls=std, dom_values={std[0].backend_node_id: "Yes"})
    fd34 = {
        "label": "Do you have a valid work permit?",
        "value": "Yes",
        "required": True,
        "kind": "radio",
        "llm": fake_llm,
    }
    out34 = await observe_act(fs34, fd34)
    tr34 = fd34.get("_trace") or []
    chk(
        "STANDARD radio reads DOM group -> NO visual path, NO marks VLM spend",
        out34 == DONE
        and not any(t.startswith("visual-choice") for t in tr34)
        and "choice-no-group" not in tr34
        and fs34.vlm_calls == 0,
        (out34, fs34.vlm_calls, tr34),
    )

    # ===== PROVEN-PATH DELEGATION (the abstraction-layer reuse) =====
    # When the run carries a per-archetype adapter, the COMMIT is the adapter's proven
    # fill()+read_back() and the generic engine is the FALLBACK. These four cases pin the contract:
    # verified proven commit short-circuits the generic engine; an unverified / missed / file-field
    # commit degrades cleanly to the generic path.
    class _FakeAdapter:
        def __init__(self, fill_ok: bool, read_ok: bool):
            self._fill_ok, self._read_ok = fill_ok, read_ok
            self.fill_calls, self.read_calls = 0, 0

        async def fill(self, _session, _page, _field, _value, _resume):
            self.fill_calls += 1
            return self._fill_ok

        async def read_back(self, _session, _page, _field, _value):
            self.read_calls += 1
            return self._read_ok

    _fobj = _mk(tag="select", ax_name="Country")  # any FormField-stand-in; the fake adapter ignores it
    _fpage = object()

    # (35) adapter.fill TRUE + read_back TRUE -> DONE via proven path, generic locate NEVER runs.
    ad35 = _FakeAdapter(fill_ok=True, read_ok=True)
    fs35 = FakeSession(controls=[])  # no controls: if generic locate ran it would no-control->ESCALATE
    fd35 = {
        "label": "What is your current country of residence?",
        "value": "United States",
        "required": True,
        "adapter": ad35,
        "page": _fpage,
        "field_obj": _fobj,
        "llm": fake_llm,
    }
    out35 = await observe_act(fs35, fd35)
    tr35 = fd35.get("_trace") or []
    chk(
        "PROVEN commit: adapter.fill+read_back -> DONE, generic engine NOT entered",
        out35 == DONE
        and "adapter:DONE" in tr35
        and "S1_LOCATE" not in tr35
        and fd35.get("_committed") == "United States"
        and ad35.fill_calls == 1
        and ad35.read_calls == 1,
        (out35, tr35),
    )

    # (36) adapter.fill TRUE but read_back FALSE -> fall through to the generic engine (which then
    #      binds + DOM-verifies the value the adapter actually wrote). Proves unconfirmed != failure.
    ad36 = _FakeAdapter(fill_ok=True, read_ok=False)
    name36 = _mk(tag="input", typ="text", ax_name="Pronouns")
    fs36 = FakeSession(controls=[name36], dom_values={name36.backend_node_id: "they/them"})
    fd36 = {
        "label": "Your Pronouns",
        "value": "they/them",
        "required": True,
        "adapter": ad36,
        "page": _fpage,
        "field_obj": _fobj,
        "llm": fake_llm,
    }
    out36 = await observe_act(fs36, fd36)
    tr36 = fd36.get("_trace") or []
    chk(
        "PROVEN unconfirmed: read_back False -> generic engine takes over -> DONE",
        out36 == DONE and "adapter.read_back:unconfirmed->generic" in tr36 and "S1_LOCATE" in tr36,
        (out36, tr36),
    )

    # (37) adapter.fill FALSE (proven path could not commit) -> generic engine takes over.
    ad37 = _FakeAdapter(fill_ok=False, read_ok=False)
    name37 = _mk(tag="input", typ="text", ax_name="First Name")
    fs37 = FakeSession(controls=[name37], dom_values={name37.backend_node_id: "Pyry"})
    fd37 = {
        "label": "First Name",
        "value": "Pyry",
        "required": True,
        "adapter": ad37,
        "page": _fpage,
        "field_obj": _fobj,
        "llm": fake_llm,
    }
    out37 = await observe_act(fs37, fd37)
    tr37 = fd37.get("_trace") or []
    chk(
        "PROVEN miss: adapter.fill False -> generic engine takes over -> DONE",
        out37 == DONE and "adapter.fill:miss->generic" in tr37 and ad37.read_calls == 0 and "S1_LOCATE" in tr37,
        (out37, tr37),
    )

    # (38) FILE field (resume set) -> proven delegation is SKIPPED (the dropzone upload is the
    #      documented renderer-freeze); the dedicated global-file path keeps ownership. No S_ADAPTER.
    ad38 = _FakeAdapter(fill_ok=True, read_ok=True)
    hidden_file38 = _mk(tag="input", typ="file", ax_name="")
    fs38 = FakeSession(controls=[hidden_file38])
    fd38 = {
        "label": "Resume",
        "value": "",
        "required": True,
        "resume": "/tmp/resume.pdf",
        "adapter": ad38,
        "page": _fpage,
        "field_obj": _fobj,
        "llm": fake_llm,
    }
    out38 = await observe_act(fs38, fd38)
    tr38 = fd38.get("_trace") or []
    chk(
        "FILE field bypasses proven delegation (global file path owns it) -> no S_ADAPTER, fill not called",
        "S_ADAPTER" not in tr38 and ad38.fill_calls == 0,
        (out38, tr38),
    )

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
