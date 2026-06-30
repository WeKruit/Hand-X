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
            if isinstance(parsed, (list, tuple)):
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


class FakeSession:
    """The offline stand-in for browser_use.BrowserSession used by the state machine tests."""

    def __init__(
        self,
        *,
        controls: list[Any],
        on_click_delta: dict[int, list[tuple[str, tuple[int, int]]]] | None = None,
        on_type_delta: dict[int, list[tuple[str, tuple[int, int]]]] | None = None,
        read_options_map: dict[int, list[str]] | None = None,
        verdict: str = '{"filled": true, "matches": true}',
        verdict_sequence: list[str] | None = None,
        url: str = "https://example.test/apply",
    ) -> None:
        self._base = {c.backend_node_id: c for c in controls}
        self._live: dict[int, Any] = dict(self._base)
        self._on_click = on_click_delta or {}
        self._on_type = on_type_delta or {}
        self._read_options = read_options_map or {}
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

    # -- perception entrypoint --------------------------------------------------
    async def get_browser_state_summary(self, *, include_screenshot: bool = False, cached: bool = False) -> Any:
        return _FakeSummary(dict(self._live), self._url)

    async def get_current_page_url(self) -> str:
        return self._url

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


# --------------------------------------------------------------------------- #
# Patch vision_verify.visual_check so brain.verify returns the FakeSession's
# scripted verdict — exercising route_verdict / S_VERIFY routing at $0.
# Importing this module installs the patch (idempotent).
# --------------------------------------------------------------------------- #
async def _fake_visual_check(
    session: Any, target: str, *, want: Any = None, key: Any = None, use_cache: bool = True
) -> str:
    if isinstance(session, FakeSession):
        return session.next_verdict()
    # unknown session in a test -> a neutral "correct" so nothing hangs
    return '{"filled": true, "matches": true}'


# install once
if getattr(_brain._vv.visual_check, "__name__", "") != "_fake_visual_check":
    _brain._vv.visual_check = _fake_visual_check  # type: ignore[assignment]
