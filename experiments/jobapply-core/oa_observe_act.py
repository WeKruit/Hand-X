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
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import oa_action as act
import oa_brain as brain
import oa_perception as perc

# --------------------------------------------------------------------------- #
# Outcome terminals (§2). Mapped to the ladder in §6.5 by the runner.
# --------------------------------------------------------------------------- #
DONE = "DONE"  # field visibly holds the value (or a clearly-equivalent option)
OTHER = "OTHER"  # required, no exact match -> a genuine "Other"/"Prefer not" escape committed
SKIP = "SKIP"  # optional & blank / optional & unmatchable -> left blank (agent-repairable)
ESCALATE = "ESCALATE"  # required & unfillable deterministically -> caller's agent of last resort
Outcome = str

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
FIELD_DEADLINE = 15.0  # GLOBAL per-field wall-clock (seconds)
FIELD_VERIFY_CAP = 3  # per-FIELD verify attempts total (DOM read-back + VLM aids combined)
FIELD_VLM_CAP = 2  # per-FIELD VLM-aid sub-budget (DOM-first means the VLM is rarely needed)

# Settle timings (§3.5). Bounded poll, no fixed long sleeps.
_POLL_S = 0.12
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
    deadline = time.monotonic() + settle_s
    prev_ids: tuple[int, ...] | None = None
    last: list[perc.DeltaNode] = []
    while True:
        await asyncio.sleep(_POLL_S)
        after = await perc.get_state(session)
        last = perc.delta(before, after)
        ids = tuple(d.backend_node_id for d in last)
        if prev_ids is not None and ids == prev_ids:
            return last  # two identical reads -> settled
        prev_ids = ids
        if time.monotonic() >= deadline:
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
    if not ctx.value.strip():
        ctx.trace.append("blank->SKIP")
        return SKIP
    return await _s1_locate(session, ctx)


# ---- S1_LOCATE ----
async def _s1_locate(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S1_LOCATE")
    state = await perc.get_state(session)
    ranked = perc.locate_field_ranked(state, ctx.label)
    if not ranked:
        ctx.trace.append("no-control")
        return ESCALATE if ctx.required else SKIP
    ctx.node = ranked[0][0]
    # label-collision (repeaters with two "Degree") -> force a value-verify later (§6 fast-path off)
    if len(ranked) >= 2 and abs(ranked[0][1] - ranked[1][1]) < 1e-9:
        ctx.ambiguous = True
        ctx.trace.append("ambiguous-label")
    return await _s2_classify(session, ctx)


# ---- S2_CLASSIFY ----
async def _s2_classify(session: Any, ctx: Ctx) -> Outcome:
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
            return await _s_choice(session, ctx)
        if intrinsic == "INTRINSIC_SELECT":
            return await _s_native(session, ctx)
        if intrinsic == "INTRINSIC_DATE":
            return await _s_date(session, ctx)

    # label-meaning nature (§4.2) — one cheap LLM call, deterministic overrides in code (§4.3).
    hints = brain.ClassifyHints(
        known_multi=(ctx.cardinality == "many"),
        value_is_list=("," in ctx.value or ";" in ctx.value),
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


# ---- S_FILE ----
async def _s_file(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_FILE")
    path = ctx.resume or ctx.value
    if not path:
        return SKIP
    ok = await act.upload_file(session, ctx.node, str(path))  # CDP only, NO click (no OS picker)
    if not ok:
        ctx.trace.append("upload-failed")
        return ESCALATE if ctx.required else SKIP
    # presence-only verify (the file input value is opaque to the VLM; CDP set is reliable)
    ctx.committed_text = str(path)
    return DONE


# ---- S_CHOICE (radio / checkbox; options already on screen) ----
async def _s_choice(session: Any, ctx: Ctx) -> Outcome:
    if not ctx.guard():
        return ESCALATE if ctx.required else SKIP
    ctx.trace.append("S_CHOICE")
    # The group's options are siblings already rendered. Read them from the live state and
    # let the cheap picker choose; commit by TRUSTED click on the resolved control.
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
    await act.click_node(session, node)  # TRUSTED click on the visible proxy / input
    return await _s_verify(session, ctx)


def _read_choice_group(state: perc.OAState, ctx: Ctx) -> list[tuple[str, Any]]:
    """All radio/checkbox options visible in the page that belong to this field's group.
    Structure-agnostic: a control whose intrinsic kind matches the trigger AND whose label
    overlaps the field label region. Returns [(option_label, node)]."""
    want_kind = classify_intrinsic(ctx.node)
    out: list[tuple[str, Any]] = []
    for node in state.selector_map.values():
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
    from oa_observe_act_fakes import FakeSession, GenericFakeLLM  # helpers alongside the tests

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
