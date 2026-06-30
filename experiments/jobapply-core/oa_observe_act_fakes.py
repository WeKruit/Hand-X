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
        self._verdict = verdict
        self._vseq = list(verdict_sequence or [])
        self._url = url
        self.event_bus = _FakeBus(self)

        # records for assertions
        self.last_click_text: str | None = None
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

    # -- DOM read-back entrypoint (oa_dom_value.read_dom_value) ------------------
    # read_dom_value resolves a node to an objectId then callFunctionOn-reads its value. We serve
    # the scripted dom_values[backend_node_id] (default "" -> visual-only, falls to the VLM aid).
    async def cdp_client_for_node(self, node: Any) -> Any:
        from oa_dom_value import _FakeCdpSend, _FakeCdpSession

        bnid = getattr(node, "backend_node_id", None)
        val = self._dom_values.get(bnid, "")
        return _FakeCdpSession(_FakeCdpSend(object_id="obj", value=val))

    # -- verify entrypoints (only reached if visual_check is NOT patched) --------
    async def take_screenshot(self) -> bytes:
        return b"\x89PNG\r\n"

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
