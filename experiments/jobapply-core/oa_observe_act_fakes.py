"""oa_observe_act_fakes — OFFLINE test doubles for the observe_act state machine.

A `FakeSession` that emulates exactly the slice of browser-use's `BrowserSession` the
three foundation modules touch, so the WHOLE state machine runs with no browser, no
network, and no VLM spend ($0):

  * oa_perception.get_state  -> `session.get_browser_state_summary(...)` returning a
    summary with `.dom_state.selector_map` (scriptable: a click/type mounts a delta cluster).
  * oa_action.*              -> `session.event_bus.dispatch(Event)` -> awaitable -> `.event_result()`.
    We record every click/type/select/upload and return the watchdog-shaped dicts.
  * oa_brain.verify          -> patched at import time so `vision_verify.visual_check` returns a
    SCRIPTED verdict string instead of calling a real VLM (routing is exercised at $0).

This lives in its own module (not under __main__) so both oa_observe_act._selftest and any
future pytest can import it. It is test-only; production never imports it.
"""

from __future__ import annotations

import json
from typing import Any

import oa_brain as _brain
import oa_perception as _perc


# --------------------------------------------------------------------------- #
# A generic offline LLM: classify by label keyword, variants by acronym table,
# pick by token-overlap of the wanted value against the option list. No network.
# Smarter than oa_brain._FakeLLM (which is hardcoded for that module's own test).
# --------------------------------------------------------------------------- #
class _Completion:
    def __init__(self, obj: Any) -> None:
        self.completion = obj


class GenericFakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages: Any, output_format: Any = None) -> _Completion:
        self.calls += 1
        name = getattr(output_format, "__name__", "")
        payload = ""
        for m in messages:
            payload = str(getattr(m, "content", "")) or payload
        low = payload.lower()
        if name == "_Nat":
            if any(k in low for k in ("why do you want", "cover letter", "linkedin", "describe", "tell us")):
                return _Completion(output_format(nature="free_text", cardinality="one"))
            if any(k in low for k in ("school", "university", "city", "location", "skill", "employer")):
                card = "many" if "skill" in low else "one"
                return _Completion(output_format(nature="searchable", cardinality=card))
            if any(k in low for k in ("degree", "gender", "state", "how did you hear", "race", "ethnic")):
                return _Completion(output_format(nature="closed_list", cardinality="one"))
            return _Completion(output_format(nature="free_text", cardinality="one"))
        if name == "_Vars":
            if "ucla" in low:
                return _Completion(output_format(variants=["University of California, Los Angeles", "UCLA"]))
            return _Completion(output_format(variants=[]))
        if name == "_Pick":
            wanted, opts = _parse_pick_payload(payload)
            return _Completion(output_format(choice=_best_match(wanted, opts)))
        raise AssertionError(f"unexpected output_format {name!r}")


def _parse_pick_payload(payload: str) -> tuple[str, list[str]]:
    wanted = ""
    opts: list[str] = []
    if "wanted:" in payload:
        wanted = payload.split("wanted:")[1].split("\n")[0].strip().strip("'\"")
    if "options:" in payload:
        tail = payload.split("options:")[-1].strip()
        try:
            import ast

            parsed = ast.literal_eval(tail)
            if isinstance(parsed, list | tuple):
                opts = [str(x) for x in parsed]
        except Exception:
            opts = []
    return wanted, opts


def _best_match(wanted: str, options: list[str]) -> str:
    """Token-overlap pick: the option sharing the most tokens with the wanted value, or
    a known abbreviation expansion (UCLA, BS->Bachelor). 'NONE' if nothing overlaps."""
    if not options:
        return "NONE"
    w = _perc._tokens(wanted)
    abbrev = {
        "bs": "bachelor",
        "ba": "bachelor",
        "ms": "master",
        "ma": "master",
        "phd": "doctor",
        "ucla": "angeles",
    }
    extra = {abbrev[t] for t in (w or set()) if t in abbrev}
    w = w | extra
    best, best_n = "NONE", 0
    for o in options:
        n = len(w & _perc._tokens(o))
        if n > best_n:
            best, best_n = o, n
    return best if best_n > 0 else "NONE"


# --------------------------------------------------------------------------- #
# A scriptable browser state. selector_map is the live world; clicking/typing a
# control mutates it to add that control's scripted delta cluster (new node ids).
# --------------------------------------------------------------------------- #
class _FakeSummary:
    def __init__(self, selector_map: dict[int, Any], url: str) -> None:
        self.dom_state = type("DS", (), {"selector_map": selector_map})()
        self.url = url
        self.title = ""


class _FakeEvent:
    """Mimics browser-use's dispatched-event handle: awaitable, then `.event_result()`."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def __await__(self):
        async def _noop():
            return None

        return _noop().__await__()

    async def event_result(self, *, raise_if_any: bool = False, raise_if_none: bool = False) -> Any:
        return self._result


class _FakeBus:
    def __init__(self, session: FakeSession) -> None:
        self._s = session

    def dispatch(self, event: Any) -> _FakeEvent:
        return self._s._handle(event)


def _opt_node(text: str, center: tuple[int, int]) -> Any:
    from browser_use.dom.views import DOMRect, EnhancedAXNode, EnhancedDOMTreeNode, NodeType

    _opt_node._n += 1  # type: ignore[attr-defined]
    nid = 10_000 + _opt_node._n  # high ids so they never collide with controls
    ax = EnhancedAXNode(
        ax_node_id=f"ax{nid}",
        ignored=False,
        role="option",
        name=text,
        description=None,
        properties=None,
        child_ids=None,
    )
    cx, cy = center
    return EnhancedDOMTreeNode(
        node_id=nid,
        backend_node_id=nid,
        node_type=NodeType.ELEMENT_NODE,
        node_name="DIV",
        # aria-label carries the text too: browser-use's event models re-validate the node and
        # drop the nested ax_node, but plain attributes survive — so node_option_text still reads it.
        node_value="",
        attributes={"role": "option", "aria-label": text},
        is_scrollable=False,
        is_visible=True,
        absolute_position=DOMRect(x=cx - 50, y=cy - 14, width=100, height=28),
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


_opt_node._n = 0  # type: ignore[attr-defined]


def make_card(question: str, *, input_bnid: int, role: str = "textbox", box=(100, 240, 220, 32)) -> Any:
    """Build a LEVER-STYLE custom question-card: a wrapper holding the visible QUESTION text and an
    input that is NOT wired to it (no <label for>, no aria-label == the question). The input's
    accessible name is blank, so Tier-1 structure CANNOT bind it — only Tier-2 spatial can, by
    reading the wrapper's text via ``get_all_children_text`` (the real browser-use API). Returns the
    input node (with parent_node/children_nodes linkage populated, exactly like the live serializer)."""
    from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType

    x, y, w, h = box
    # the visible question text node (non-interactive — lives in the tree, not the selector_map).
    text_node = EnhancedDOMTreeNode(
        node_id=input_bnid * 10 + 1,
        backend_node_id=input_bnid * 10 + 1,
        node_type=NodeType.TEXT_NODE,
        node_name="#text",
        node_value=question,
        attributes={},
        is_scrollable=False,
        is_visible=True,
        absolute_position=DOMRect(x=x, y=y - 24, width=w, height=18),
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[],
        ax_node=None,
        snapshot_node=None,
    )
    # the input — NO ax name matching the question (the un-wired card input).
    inp = EnhancedDOMTreeNode(
        node_id=input_bnid,
        backend_node_id=input_bnid,
        node_type=NodeType.ELEMENT_NODE,
        node_name="INPUT",
        node_value="",
        attributes={"type": "text", "role": role},
        is_scrollable=False,
        is_visible=True,
        absolute_position=DOMRect(x=x, y=y, width=w, height=h),
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[],
        ax_node=None,
        snapshot_node=None,
    )
    # the card wrapper holding BOTH (so _climb_wrapper finds it and get_all_children_text reads Q).
    wrapper = EnhancedDOMTreeNode(
        node_id=input_bnid * 10 + 2,
        backend_node_id=input_bnid * 10 + 2,
        node_type=NodeType.ELEMENT_NODE,
        node_name="DIV",
        node_value="",
        attributes={"class": "application-question"},
        is_scrollable=False,
        is_visible=True,
        absolute_position=DOMRect(x=x, y=y - 24, width=w, height=h + 24),
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[text_node, inp],
        ax_node=None,
        snapshot_node=None,
    )
    text_node.parent_node = wrapper
    inp.parent_node = wrapper
    return inp


def _text_node(bnid: int, text: str, box: tuple[int, int, int, int]) -> Any:
    """A non-interactive heading text node (lives in the tree, NOT in the selector_map)."""
    from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType

    x, y, w, h = box
    return EnhancedDOMTreeNode(
        node_id=bnid,
        backend_node_id=bnid,
        node_type=NodeType.TEXT_NODE,
        node_name="#text",
        node_value=text,
        attributes={},
        is_scrollable=False,
        is_visible=True,
        absolute_position=DOMRect(x=x, y=y, width=w, height=h),
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[],
        ax_node=None,
        snapshot_node=None,
    )


def _control_node(
    bnid: int, *, tag: str, typ: str | None, role: str | None, box, visible=True, ax_name: str | None = None
) -> Any:
    """A fillable control. ``ax_name`` is the control's OWN accessible name: for a radio/checkbox option
    it is the OPTION text ('Yes'/'No') — browser-use resolves the wrapping <label> into the ax name, so
    options ARE named, but the QUESTION HEADING is a separate un-wired card node above them (the gap).
    For a textarea/select card we leave ax_name blank (the heading is the only text), so Tier-1 cannot
    bind it and only the grouped-widget card-heading tier can."""
    from browser_use.dom.views import DOMRect, EnhancedAXNode, EnhancedDOMTreeNode, NodeType

    attrs: dict[str, str] = {}
    if typ:
        attrs["type"] = typ
    if role:
        attrs["role"] = role
    ax = None
    if ax_name is not None:
        ax = EnhancedAXNode(
            ax_node_id=f"ax{bnid}",
            ignored=False,
            role=role,
            name=ax_name,
            description=None,
            properties=None,
            child_ids=None,
        )
    x, y, w, h = box
    return EnhancedDOMTreeNode(
        node_id=bnid,
        backend_node_id=bnid,
        node_type=NodeType.ELEMENT_NODE,
        node_name=tag.upper(),
        node_value="",
        attributes=attrs,
        is_scrollable=False,
        is_visible=visible,
        absolute_position=DOMRect(x=x, y=y, width=w, height=h),
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


def _wrap(bnid: int, children: list[Any], box) -> Any:
    """A card/section DIV wrapper holding a heading + control rows, with parent/child linkage set
    exactly like the live serializer (so ``get_all_children_text`` reads the heading + option text)."""
    from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType

    x, y, w, h = box
    node = EnhancedDOMTreeNode(
        node_id=bnid,
        backend_node_id=bnid,
        node_type=NodeType.ELEMENT_NODE,
        node_name="DIV",
        node_value="",
        attributes={},
        is_scrollable=False,
        is_visible=True,
        absolute_position=DOMRect(x=x, y=y, width=w, height=h),
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=children,
        ax_node=None,
        snapshot_node=None,
    )
    for c in children:
        c.parent_node = node
    return node


def make_choice_card(
    question: str,
    options: list[str],
    *,
    base_bnid: int,
    kind: str = "radio",
    visible: bool = True,
    top: int = 240,
) -> list[Any]:
    """LEVER-STYLE grouped radio/checkbox card: a card holding a heading + N option ROWS, each row a
    ``<label>`` wrapping the option ``<input type=radio|checkbox>`` (blank accessible name) and the
    option's own text node ('Yes'/'No'). The heading is NOT wired to any input. Returns the list of
    OPTION CONTROL nodes to drop into the selector_map (the heading/rows/card live only in the tree,
    reachable from each control's ``parent_node`` chain — exactly the live serializer shape)."""
    heading = _text_node(base_bnid, question, (100, top, 360, 18))
    rows: list[Any] = [heading]
    controls: list[Any] = []
    for i, opt in enumerate(options):
        cid = base_bnid + 1 + i
        oy = top + 28 + i * 30
        ctrl = _control_node(cid, tag="input", typ=kind, role=kind, box=(100, oy, 18, 18), visible=visible, ax_name=opt)
        otext = _text_node(cid * 100 + 7, opt, (124, oy, 120, 18))
        row = _wrap(cid * 1000 + 3, [ctrl, otext], (100, oy, 240, 24))
        rows.append(row)
        controls.append(ctrl)
    _wrap(base_bnid * 1000 + 9, rows, (100, top - 4, 380, 28 + 30 * len(options)))
    return controls


def make_labelfree_choice_card(
    heading_text: str,
    options: list[str],
    *,
    base_bnid: int,
    kind: str = "radio",
    top: int = 240,
) -> list[Any]:
    """A radio/checkbox card whose visible HEADING shares NO tokens with the question we search for —
    e.g. an imaged/rich heading rendered as unrelated alt text ('Work eligibility' for the question
    'Are you authorized to work?'). Structure (radios named 'Yes'/'No'), shallow spatial, AND the
    grouped-text card tier all MISS (the heading does not name the field), so ONLY the VISUAL
    set-of-marks tier can bind it. Same DOM shape as ``make_choice_card`` — returns the option controls.
    """
    return make_choice_card(heading_text, options, base_bnid=base_bnid, kind=kind, top=top)


def make_custom_choice_card(
    question: str,
    options: list[str],
    *,
    base_bnid: int,
    visible: bool = True,
    top: int = 240,
) -> list[Any]:
    """A LEVER-LIVE custom radio/checkbox card whose option controls DO NOT self-identify as
    intrinsic radio/checkbox — a styled ``<div>``/``<a>`` proxy with NO ``type``/``role`` standard
    attribute (only its visible option text). This is the proven live shape where ``classify_intrinsic``
    returns "" and the label-LLM mis-derives BOOLEAN -> S3_OPEN -> search -> 0 opts -> ESCALATE.
    The ADAPTER's ``kind`` hint ('radio'/'checkbox') is the ONLY signal that routes it to S_CHOICE.
    Returns the option control nodes (heading/rows/card live in the tree via parent_node)."""
    heading = _text_node(base_bnid, question, (100, top, 360, 18))
    rows: list[Any] = [heading]
    controls: list[Any] = []
    for i, opt in enumerate(options):
        cid = base_bnid + 1 + i
        oy = top + 28 + i * 30
        # A styled custom proxy that IS fillable (so the locate binds it) but does NOT self-identify
        # as an intrinsic radio/checkbox: a text-typed input with NO radio/checkbox type or role. Its
        # ax name is the option text (a human reads 'Yes'/'No'), but classify_intrinsic(node) == "" —
        # exactly the live Lever shape where only the adapter's ctx.kind can route + scope the group.
        ctrl = _control_node(
            cid, tag="input", typ="text", role=None, box=(100, oy, 18, 18), visible=visible, ax_name=opt
        )
        otext = _text_node(cid * 100 + 7, opt, (124, oy, 120, 18))
        row = _wrap(cid * 1000 + 3, [ctrl, otext], (100, oy, 240, 24))
        rows.append(row)
        controls.append(ctrl)
    _wrap(base_bnid * 1000 + 9, rows, (100, top - 4, 380, 28 + 30 * len(options)))
    return controls


def make_custom_select_card(
    question: str,
    *,
    bnid: int,
    visible: bool = True,
    top: int = 240,
) -> Any:
    """A LEVER-LIVE custom single_select card: a heading block + a combobox-style TEXT input trigger
    (role=combobox, aria-autocomplete=list) that is NOT a native <select> — so ``read_options``
    returns [] and a bare click mounts no delta (the options render only after a filter keystroke).
    The adapter's ``kind='single_select'`` hint routes it to the open+TYPE-TO-FILTER+read path.
    Returns the trigger control node (heading/wrappers live in the tree via parent_node)."""
    heading_text = _text_node(bnid * 10 + 1, question, (100, top, 360, 18))
    heading_block = _wrap(bnid * 10 + 3, [heading_text], (100, top, 360, 20))
    ctrl = _control_node(
        bnid,
        tag="input",
        typ="text",
        role="combobox",
        box=(100, top + 28, 320, 32),
        visible=visible,
        ax_name="",
    )
    ctrl.attributes["aria-autocomplete"] = "list"  # a filtering combobox, not a native select
    field_wrap = _wrap(bnid * 10 + 4, [ctrl], (100, top + 24, 360, 40))
    _wrap(bnid * 10 + 2, [heading_block, field_wrap], (100, top - 4, 380, 80))
    return ctrl


def make_visual_radio_card(
    question: str,
    options: list[str],
    *,
    base_bnid: int,
    top: int = 240,
) -> list[Any]:
    """A LEVER-LIVE custom radio card whose OPTIONS are styled non-fillable DIVs (no input/role) — the
    exact shape proven from runs/cards/lever.json: located:grouped -> S_CHOICE -> ``_read_choice_group``
    finds NO options (the styled divs are not <input type=radio>, and the bound trigger has no label) ->
    the VISUAL fallback must SEE the option divs + click by coordinate.

    Card layout: heading text + a VISIBLE fillable TRIGGER input with NO accessible name (so locate
    binds the card via the grouped-widget tier through this trigger, but ``_read_choice_group`` yields
    an empty group because no visible FILLABLE control carries an option label), followed by N visible
    styled option DIVs (role-less, ax name = the option text, each with an on-screen box inside the
    card). Returns [trigger, *option_divs] — ALL must go into the selector_map so the set-of-marks can
    mark the option divs by backend_node_id. The trigger is controls[0]; option divs follow in order."""
    # The heading is its OWN block (a separate sibling section above the control wrapper) — mirrors the
    # real Lever shape, so the shallow ``_climb_wrapper`` stops at the inner field-wrapper (no heading)
    # and Tier-2 SPATIAL cannot bind it; only the grouped-widget card tier reaches the heading and binds
    # with ``ctx.card`` SET (which scopes ``_read_choice_group`` to the card's fillable controls).
    heading_text = _text_node(base_bnid, question, (100, top, 360, 18))
    heading_block = _wrap(base_bnid * 10 + 3, [heading_text], (100, top, 360, 20))
    # the bound trigger: fillable + visible but UNLABELED (binds the card, contributes no group option)
    trigger = _control_node(
        base_bnid + 1, tag="input", typ="text", role="combobox", box=(100, top + 26, 18, 18), visible=True, ax_name=""
    )
    field_kids: list[Any] = [trigger]
    option_divs: list[Any] = []
    for i, opt in enumerate(options):
        did = base_bnid + 10 + i
        oy = top + 28 + i * 30
        # a styled NON-FILLABLE option proxy: a DIV with NO type/role but the option text as its ax
        # name — classify_intrinsic == "" and _is_fillable_control == False, so the group read skips it,
        # but it IS visible with a box, so the VISUAL marks tier can number + click it.
        div = _control_node(did, tag="div", typ=None, role=None, box=(124, oy, 120, 18), visible=True, ax_name=opt)
        field_kids.append(div)
        option_divs.append(div)
    # field-wrapper: trigger + option divs (>1 child, but its text is only the option labels 'Yes'/'No',
    # NOT the heading) — so ``_group_text(trigger)`` does NOT name the field and spatial misses.
    field_wrap = _wrap(base_bnid * 10 + 4, field_kids, (100, top + 24, 360, 24 + 30 * len(options)))
    _wrap(base_bnid * 10 + 2, [heading_block, field_wrap], (100, top - 4, 380, 60 + 30 * len(options)))
    return [trigger, *option_divs]


def make_single_input_card(
    question: str,
    *,
    bnid: int,
    tag: str = "textarea",
    typ: str | None = None,
    role: str | None = None,
    visible: bool = True,
    top: int = 240,
) -> Any:
    """A card with a heading BLOCK + a SINGLE un-wired control nested in its own field-wrapper — the
    real Lever shape where the heading is a separate sibling section above the control's wrapper, so
    the shallow ``_climb_wrapper`` stops at the inner field-wrapper (no heading) and ONLY the grouped-
    widget card tier reaches the heading via ``get_all_children_text``. Returns the control node."""
    heading_text = _text_node(bnid * 10 + 1, question, (100, top, 360, 18))
    heading_block = _wrap(bnid * 10 + 3, [heading_text], (100, top, 360, 20))  # heading is its own block
    ctrl = _control_node(bnid, tag=tag, typ=typ, role=role, box=(100, top + 28, 320, 32), visible=visible)
    # the control's own field-wrapper holds the control + an adornment (clear icon) — >1 child but NO
    # heading, so the shallow _climb_wrapper STOPS here and reads no heading; only the grouped card tier
    # (which climbs to the section whose text contains the heading) can bind it. Mirrors the real shape.
    icon = _text_node(bnid * 10 + 5, "", (420, top + 28, 16, 16))  # a non-heading adornment node
    field_wrap = _wrap(bnid * 10 + 4, [ctrl, icon], (100, top + 24, 360, 40))
    _wrap(bnid * 10 + 2, [heading_block, field_wrap], (100, top - 4, 380, 80))
    return ctrl


def _hidden_file_input(bnid: int, *, name: str = "resume") -> Any:
    """A HIDDEN / zero-box ``input[type=file]`` — the normal shape on GH/Lever/Ashby (a styled
    'Attach' button proxies for it). is_visible=False + a zero box, so the label ranker can't see
    it; only the GLOBAL file path (which scans ALL file inputs incl. hidden) reaches it."""
    from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType

    return EnhancedDOMTreeNode(
        node_id=bnid,
        backend_node_id=bnid,
        node_type=NodeType.ELEMENT_NODE,
        node_name="INPUT",
        node_value="",
        attributes={"type": "file", "name": name},
        is_scrollable=False,
        is_visible=False,
        absolute_position=DOMRect(x=0, y=0, width=0, height=0),
        target_id="t",
        frame_id=None,
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[],
        ax_node=None,
        snapshot_node=None,
    )


# --------------------------------------------------------------------------- #
# A fake CDP session that backs the DIRECT-CDP action backend (oa_cdp_action, the DEFAULT) offline.
# It interprets exactly the CDP calls oa_cdp_action issues:
#   DOM.resolveNode        -> {object:{objectId}}        (resolve always succeeds for a real bnid)
#   Runtime.callFunctionOn -> reads the JS body to decide:
#       * read oracle (_READ_VALUE_JS)  -> the scripted read_value
#       * set value (_SET_VALUE_JS)     -> echoes the arg (success) + records last_type_text
#       * select   (_SELECT_JS)         -> returns the wanted text + records last_select_text
#       * this.click() (_JS_CLICK_JS)   -> records last_click_text for an option node, True
#       * getBoundingClientRect (_RECT_JS) -> the node's box (so cdp_click takes the mouse path)
#   DOM.focus              -> noop
#   Input.dispatchMouseEvent / dispatchKeyEvent -> record a click / type + mount on-click/on-type delta
# This routes the CDP backend onto the SAME FakeSession recorders the event-bus _handle uses, so the
# state-machine self-test asserts identically on either backend. $0, no browser, no network.
# --------------------------------------------------------------------------- #
class _FakeCdpActionSession:
    def __init__(self, owner: FakeSession, node: Any, *, read_value: str) -> None:
        self.session_id = "sess-fake"
        self._owner = owner
        self._node = node
        self._bnid = getattr(node, "backend_node_id", None)
        self._read_value = read_value
        self._typed = ""  # accumulates keystrokes for a typeahead type (per-char dispatch)
        self.cdp_client = type("C", (), {"send": self._Send(self)})()

    class _Send:
        def __init__(self, sess: _FakeCdpActionSession) -> None:
            outer = sess

            async def _resolve(params: Any = None, session_id: Any = None) -> dict:
                return {"object": {"objectId": f"obj-{outer._bnid}"}}

            async def _call(params: Any = None, session_id: Any = None) -> dict:
                fn = (params or {}).get("functionDeclaration", "")
                args = [a.get("value") for a in (params or {}).get("arguments", [])]
                return {"result": {"value": outer._run_js(fn, args)}}

            async def _focus(params: Any = None, session_id: Any = None) -> dict:
                return {}

            async def _mouse(params: Any = None, session_id: Any = None) -> dict:
                outer._on_mouse(params or {})
                return {}

            async def _key(params: Any = None, session_id: Any = None) -> dict:
                outer._on_key(params or {})
                return {}

            self.DOM = type("DOM", (), {"resolveNode": staticmethod(_resolve), "focus": staticmethod(_focus)})()
            self.Runtime = type("Runtime", (), {"callFunctionOn": staticmethod(_call)})()
            self.Input = type(
                "Input",
                (),
                {"dispatchMouseEvent": staticmethod(_mouse), "dispatchKeyEvent": staticmethod(_key)},
            )()

    # -- JS body dispatch (callFunctionOn) -------------------------------------
    def _run_js(self, fn: str, args: list[Any]) -> Any:
        if "input[type=radio]" in fn or "querySelectorAll('select')" in fn:  # _CHOOSE_OPTION_JS /
            return ""  # _SELECT_IN_CONTAINER_JS — fake cards don't model real inputs/selects -> no match
        if "aria-owns" in fn:  # _ARIA_OPEN_JS / _ARIA_PICK_JS — fake cards have no ARIA listbox wiring
            return ""
        if "selectedIndex" in fn:  # _SELECT_JS — record the committed select text
            want = str(args[0]) if args else ""
            self._owner.last_select_text = want
            self._owner._any_write = True
            return want  # non-empty -> cdp_select True
        if "nativeSetter" in fn:  # _SET_VALUE_JS (unique marker) — echo the arg
            text = str(args[0]) if args else ""
            if text != "":  # a clear ('') must NOT overwrite last_type_text
                self._owner.last_type_text = text
                self._owner._any_write = True
                if self._bnid in self._owner._on_type:  # a value-set on a search box mounts its delta
                    self._owner._mount(self._owner._on_type[self._bnid])
            return text  # cdp_set_value success = echo == text
        if "activeElement" in fn:  # _FOCUS_IS_TARGET_JS — the fake world has no focus thief:
            return True  # DOM.focus always lands, so the guard's probe answers True
        if "this.click()" in fn:  # _JS_CLICK_JS — option-cell click commit (box-less fallback)
            self._record_click()
            self._owner._any_write = True
            return True
        if "getBoundingClientRect" in fn:  # _RECT_JS / _RECT_CENTER_JS — node box / viewport center
            rect = getattr(self._node, "absolute_position", None)
            if rect is not None and rect.width and rect.height:
                # _RECT_CENTER_JS (visual-commit node-center click) returns the CENTER {x, y}; the older
                # _RECT_JS returns the full box. Return both keys so either caller is served, and record
                # the center as the visual-commit click coordinate for the assertion.
                cx, cy = rect.x + rect.width / 2.0, rect.y + rect.height / 2.0
                if "this.scrollIntoView" in fn:  # _RECT_CENTER_JS marker
                    self._owner.last_click_xy = (int(cx), int(cy))
                    return {"x": cx, "y": cy}
                return {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}
            return None
        # default: the read oracle (_READ_VALUE_JS) -> the scripted live DOM value.
        return self._read_value

    # -- Input dispatch (trusted mouse / key) ----------------------------------
    def _on_mouse(self, params: dict) -> None:
        # cdp_click sends move/press/release; record a click on press (one logical click).
        if params.get("type") == "mousePressed":
            self._owner._any_write = True
            # cdp_click_xy (coordinate-only click, root session, _node=None) records the (x,y) so the
            # VISUAL-COMMIT fallback test can assert the engine clicked the option BY COORDINATE.
            if self._node is None and ("x" in params and "y" in params):
                self._owner.last_click_xy = (int(params["x"]), int(params["y"]))
            self._record_click()

    def _on_key(self, params: dict) -> None:
        # cdp_type keystroke path sends keyDown/char/keyUp; accumulate the chars then mount delta.
        if params.get("type") == "char":
            self._owner._any_write = True
            self._typed += str(params.get("text", ""))
            self._owner.last_type_text = self._typed
            if self._bnid in self._owner._on_type and self._typed:
                self._owner._mount(self._owner._on_type[self._bnid])

    def _record_click(self) -> None:
        node = self._node
        if self._bnid in self._owner._on_click:  # the trigger control opening a menu
            self._owner._mount(self._owner._on_click[self._bnid])
        elif node is not None and (getattr(node, "attributes", {}) or {}).get("role") == "option":
            self._owner.last_click_text = self._owner._opt_text_for_node(node)


class FakeSession:
    """The offline stand-in for browser_use.BrowserSession used by the state machine tests."""

    def __init__(
        self,
        *,
        controls: list[Any],
        on_click_delta: dict[int, list[tuple[str, tuple[int, int]]]] | None = None,
        on_type_delta: dict[int, list[tuple[str, tuple[int, int]]]] | None = None,
        read_options_map: dict[int, list[str]] | None = None,
        dom_values: dict[int, str] | None = None,
        verdict: str = '{"filled": true, "matches": true}',
        verdict_sequence: list[str] | None = None,
        url: str = "https://example.test/apply",
        marks_reply: str | None = None,
    ) -> None:
        self._base = {c.backend_node_id: c for c in controls}
        self._live: dict[int, Any] = dict(self._base)
        self._on_click = on_click_delta or {}
        self._on_type = on_type_delta or {}
        self._read_options = read_options_map or {}
        # dom_values: backend_node_id -> the live DOM value read_dom_value should return for that
        # control. Default {} -> read_dom_value returns "" (visual-only) and verify falls to the VLM
        # aid (the scripted verdict), preserving every pre-existing test's behaviour.
        self._dom_values = dom_values or {}
        # dom_values model the POST-COMMIT read (fake writes don't mutate state, so tests preset
        # the outcome). The engine's ALREADY-CORRECT pre-check reads BEFORE any interaction — reads
        # return "" until the first write so a preset outcome doesn't look like a prefilled form.
        self._any_write = False
        self._verdict = verdict
        self._vseq = list(verdict_sequence or [])
        self._url = url
        # marks_reply: the raw text the fake VLM returns for the set-of-marks pick (e.g.
        # '{"mark": 642}'). When set, ``_install_marks_vlm`` is used so brain.pick_control_by_marks
        # resolves to the node whose backend_node_id == that mark — no real VLM, no network.
        self.marks_reply = marks_reply
        self.event_bus = _FakeBus(self)

        # records for assertions
        self.last_click_text: str | None = None
        self.last_click_xy: tuple[int, int] | None = None  # coordinate of a cdp_click_xy visual commit
        self.last_select_text: str | None = None
        self.last_type_text: str | None = None
        self.last_upload: str | None = None
        self.keys: list[str] = []
        self.vlm_calls = 0  # incremented by the patched visual_check (per-field VLM accounting)
        self.state_reads = 0  # FIX 3: counts FULL-page get_state serializes (the cost we cut)

    # -- perception entrypoint --------------------------------------------------
    async def get_browser_state_summary(self, *, include_screenshot: bool = False, cached: bool = False) -> Any:
        # FIX 3 accounting: every call here is one full-page serialize — the expensive op the
        # speed fix bounds. The tests assert a normal field stays <= a few of these.
        self.state_reads += 1
        return _FakeSummary(dict(self._live), self._url)

    async def get_current_page_url(self) -> str:
        return self._url

    # -- DOM read-back + DIRECT-CDP WRITE entrypoint ----------------------------
    # Both the read oracle (oa_dom_value.read_dom_value) AND the DIRECT-CDP action backend
    # (oa_cdp_action.*, the SPA-hang fix that is now the DEFAULT) resolve a node via
    # cdp_client_for_node -> DOM.resolveNode -> Runtime.callFunctionOn / Input.*. We return ONE fake
    # CDP session serving BOTH: the scripted read value for the read oracle, AND the same write
    # side-effects the event-bus _handle records (last_type_text / last_select_text / last_click_text
    # + the scripted delta mount), so the WHOLE state machine runs identically on the CDP backend
    # with NO browser, NO network.
    async def cdp_client_for_node(self, node: Any) -> Any:
        bnid = getattr(node, "backend_node_id", None)
        val = self._dom_values.get(bnid, "")
        # pre-first-write reads are blank (see _any_write above); FILE: sentinels ARE genuinely
        # prefilled state (is_already_uploaded probes before any write).
        if not self._any_write and not str(val).startswith("FILE:"):
            val = ""
        return _FakeCdpActionSession(self, node, read_value=val)

    async def get_or_create_cdp_session(self, target_id: Any = None, focus: bool = True) -> Any:
        # root session for a coordinate-only click (oa_cdp_action.cdp_click_xy fallback path).
        return _FakeCdpActionSession(self, None, read_value="")

    async def get_element_coordinates(self, backend_node_id: int, cdp_session: Any) -> Any:
        from browser_use.dom.views import DOMRect

        node = self._live.get(backend_node_id) or self._base.get(backend_node_id)
        rect = getattr(node, "absolute_position", None) if node is not None else None
        if rect is not None and rect.width and rect.height:
            return DOMRect(x=rect.x, y=rect.y, width=rect.width, height=rect.height)
        return None

    # -- verify entrypoints (only reached if visual_check is NOT patched) --------
    async def take_screenshot(self) -> bytes:
        # A REAL minimal PNG so browser-use's set-of-marks (create_highlighted_screenshot, which
        # PIL-decodes the bytes) succeeds offline — the marks-tier locate path needs a decodable
        # image. Pre-existing tests that only patch visual_check never inspect these bytes.
        return _blank_png()

    # -- action dispatch --------------------------------------------------------
    def _mount(self, cluster: list[tuple[str, tuple[int, int]]]) -> None:
        # reset prior options, then add this control's cluster as NEW node ids (the delta)
        self._live = dict(self._base)
        for text, center in cluster:
            n = _opt_node(text, center)
            self._live[n.backend_node_id] = n

    def _opt_text_for_node(self, node: Any) -> str:
        return _perc.node_option_text(node)

    def _handle(self, event: Any) -> _FakeEvent:
        name = type(event).__name__
        node = getattr(event, "node", None)
        bnid = getattr(node, "backend_node_id", None)

        if name == "GetDropdownOptionsEvent":
            opts = self._read_options.get(bnid, [])
            payload = json.dumps([{"index": i, "text": t, "value": t, "selected": False} for i, t in enumerate(opts)])
            return _FakeEvent({"type": "dropdown", "options": payload} if opts else {})

        if name == "SelectDropdownOptionEvent":
            self.last_select_text = getattr(event, "text", None)
            return _FakeEvent({"success": "true"})

        if name == "ClickElementEvent":
            if bnid in self._on_click:
                self._mount(self._on_click[bnid])
            elif node is not None and (getattr(node, "attributes", {}) or {}).get("role") == "option":
                self.last_click_text = self._opt_text_for_node(node)
            return _FakeEvent({"clicked": True})

        if name == "ClickCoordinateEvent":
            return _FakeEvent({"clicked": True})

        if name == "TypeTextEvent":
            self.last_type_text = getattr(event, "text", None)
            if bnid in self._on_type and (self.last_type_text or ""):
                self._mount(self._on_type[bnid])
            return _FakeEvent({"typed": True})

        if name == "SendKeysEvent":
            self.keys.append(getattr(event, "keys", ""))
            return _FakeEvent(None)

        if name == "UploadFileEvent":
            self.last_upload = getattr(event, "file_path", None)
            return _FakeEvent(None)

        if name == "ScrollEvent":
            return _FakeEvent(None)

        return _FakeEvent(None)

    # -- scripted verdict for the patched visual_check --------------------------
    def next_verdict(self) -> str:
        if self._vseq:
            return self._vseq.pop(0)
        return self._verdict


class _ScrollRevealSession(FakeSession):
    """A FakeSession whose ``reveal_bnid`` control starts NOT-VISIBLE and flips visible on the first
    ScrollEvent — emulates a card below the fold that browser-use only marks visible after scrolling.
    Proves the engine's bounded scroll-into-view re-locate (``_scroll_locate``) finds a low question."""

    def __init__(self, *, reveal_bnid: int, **kw: Any) -> None:
        super().__init__(**kw)
        self._reveal_bnid = reveal_bnid

    def _handle(self, event: Any) -> _FakeEvent:
        if type(event).__name__ == "ScrollEvent":
            node = self._base.get(self._reveal_bnid)
            if node is not None:
                node.is_visible = True  # the card scrolled into the viewport -> now visible
            return _FakeEvent(None)
        return super()._handle(event)


# --------------------------------------------------------------------------- #
# Set-of-marks locate test doubles: a real blank PNG + a fake VLM that returns a
# scripted {"mark": N} reply for brain.pick_control_by_marks, so the VISUAL bind path
# (Tier-2d) is proven OFFLINE — no real VLM, no network ($0). pick_control_by_marks calls
# vision_verify._vlm().ainvoke([msg]) directly (no output_format), so we stub _vv._vlm.
# --------------------------------------------------------------------------- #
_PNG_CACHE: dict[str, bytes] = {}


def _blank_png() -> bytes:
    if "p" not in _PNG_CACHE:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (400, 300), (255, 255, 255, 255)).save(buf, format="PNG")
        _PNG_CACHE["p"] = buf.getvalue()
    return _PNG_CACHE["p"]


class _MarksReply:
    """Mimics a browser-use LLM response: a ``.completion`` string the marks parser reads."""

    def __init__(self, text: str) -> None:
        self.completion = text


class _FakeMarksVLM:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def ainvoke(self, messages: Any) -> _MarksReply:  # no output_format — matches the marks call
        return _MarksReply(self._reply)


def install_marks_vlm(reply: str) -> Any:
    """Install a fake ``vision_verify._vlm`` that returns ``reply`` (e.g. '{"mark": 642}') for the
    set-of-marks pick. Returns the ORIGINAL ``_vlm`` so the caller can restore it. The per-page VLM
    counter is bumped by pick_control_by_marks itself (same as live), so per-field budget accounting
    is exercised too."""
    orig = _brain._vv._vlm
    _brain._vv._vlm = lambda: _FakeMarksVLM(reply)  # type: ignore[assignment]
    return orig


def restore_vlm(orig: Any) -> None:
    _brain._vv._vlm = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Patch vision_verify.visual_check so brain.verify returns the FakeSession's
# scripted verdict — exercising route_verdict / S_VERIFY routing at $0.
# Importing this module installs the patch (idempotent).
# --------------------------------------------------------------------------- #
async def _fake_visual_check(
    session: Any, target: str, *, want: Any = None, key: Any = None, use_cache: bool = True
) -> str:
    # Count the VLM aid on the session AND bump vision_verify's per-page counter so the engine's
    # per-field accounting (_verify_field -> _vv_calls()) observes the spend, exactly like live.
    _brain._vv._VLM_CALLS["n"] = _brain._vv._VLM_CALLS.get("n", 0) + 1
    if isinstance(session, FakeSession):
        session.vlm_calls += 1
        return session.next_verdict()
    # unknown session in a test -> a neutral "correct" so nothing hangs
    return '{"filled": true, "matches": true}'


# install once
if getattr(_brain._vv.visual_check, "__name__", "") != "_fake_visual_check":
    _brain._vv.visual_check = _fake_visual_check  # type: ignore[assignment]
