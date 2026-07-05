"""oa_perception — thin wrapper over browser-use's DomService perception.

`observe_act` is a DETERMINISTIC orchestrator over browser-use's OWN primitives.
This module is the PERCEPTION half: it reuses browser-use's serialized DOM state
(indexed elements + absolute coords + visibility + the structure-agnostic
interactivity detection) instead of reinventing `_READ_DELTA_JS` / `mark_visible`.

Real browser-use API used (verified against the vendored tree, file:line):
  - `BrowserSession.get_browser_state_summary(include_screenshot=False, cached=False)`
    -> `BrowserStateSummary`  (browser/session.py:1535)
  - `BrowserStateSummary.dom_state: SerializedDOMState`  (browser/views.py:94)
  - `SerializedDOMState.selector_map: dict[int, EnhancedDOMTreeNode]`  (dom/views.py:934;
    the key is the node's `backend_node_id`, set at serializer.py:712)
  - `EnhancedDOMTreeNode`: `backend_node_id`, `node_name`/`tag_name`, `attributes`,
    `is_visible`, `absolute_position: DOMRect(x,y,width,height)`, `ax_node.name/.role`,
    `get_meaningful_text_for_llm()`  (dom/views.py:373-602)
  - The DELTA: browser-use's serializer computes `is_new` by diffing the current
    `backend_node_id` set against `previous_cached_state`'s selector_map
    (serializer.py:712-723). We reuse that SAME signal by diffing selector_map keys
    across two states — robust to React re-render churn since the backend_node_id is
    stable per live DOM node.

NOTE: this is perception only. No clicks, no typing, no commits live here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from browser_use.dom.views import EnhancedDOMTreeNode


# ---------------------------------------------------------------------------
# State container (thin view over browser-use's BrowserStateSummary)
# ---------------------------------------------------------------------------


@dataclass
class OAState:
    """A thin, immutable snapshot of browser-use's serialized DOM state.

    `selector_map` is browser-use's own `dict[backend_node_id -> EnhancedDOMTreeNode]`
    (the indexed, interactive, structure-agnostically-detected element set). We carry
    it verbatim so every downstream consumer works on the real node objects (real
    bounds, real ax roles), never a re-derived copy.
    """

    selector_map: dict[int, Any]  # dict[int, EnhancedDOMTreeNode]
    url: str = ""
    title: str = ""
    raw: Any = field(default=None, repr=False)  # the BrowserStateSummary OR SerializedDOMState, for callers
    # The browser-use ``SerializedDOMState`` this snapshot came from, carried so the NEXT direct
    # serialize can pass it as ``previous_cached_state`` (the serializer's own delta/is_new signal,
    # serializer.py:712-723). None on the get_browser_state_summary fallback path (the fakes).
    dom_state: Any = field(default=None, repr=False)


@dataclass
class DeltaNode:
    """A node that appeared in the after-state but not the before-state."""

    backend_node_id: int
    node: Any  # EnhancedDOMTreeNode
    text: str
    center: tuple[float, float] | None  # (cx, cy) document coords, or None if no box


# ---------------------------------------------------------------------------
# Low-level node accessors (read the REAL EnhancedDOMTreeNode fields)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
# Trailing a11y state suffixes the salesforce capture appends inside aria-label
# (e.g. "PostgreSQL not checked", "…, press delete to clear value", "X selected").
_ARIA_STATE_SUFFIX_RE = re.compile(
    r"(,?\s*(not\s+checked|checked|selected|unselected"
    r"|press\s+delete\s+to\s+clear\s+value|collapsed|expanded))+\s*$",
    re.IGNORECASE,
)


def _tag(node: Any) -> str:
    name = getattr(node, "node_name", "") or ""
    return name.lower()


def node_is_visible(node: Any) -> bool:
    """Visible per browser-use's own determination (upper-most-frame visibility)."""
    return bool(getattr(node, "is_visible", False))


def node_center(node: Any) -> tuple[float, float] | None:
    """Center point in DOCUMENT coords from the node's absolute_position rect.

    `absolute_position: DOMRect(x, y, width, height)` is browser-use's already-resolved
    document-space box (dom/views.py:407). Returns None for a zero/absent box.
    """
    rect = getattr(node, "absolute_position", None)
    if rect is None:
        return None
    w = getattr(rect, "width", 0) or 0
    h = getattr(rect, "height", 0) or 0
    if w <= 0 and h <= 0:
        return None
    return (rect.x + w / 2.0, rect.y + h / 2.0)


def node_rect(node: Any) -> tuple[float, float, float, float] | None:
    """The node's document-space box (x, y, width, height), or None if absent/zero.

    Reuses browser-use's already-resolved `absolute_position: DOMRect` (dom/views.py:407) so
    geometry is the SAME the agent sees — no re-measuring. Used by Tier-2 spatial locate to bind
    an unlabeled control to the question text sitting directly above / left of it.
    """
    rect = getattr(node, "absolute_position", None)
    if rect is None:
        return None
    w = getattr(rect, "width", 0) or 0
    h = getattr(rect, "height", 0) or 0
    if w <= 0 and h <= 0:
        return None
    return (rect.x, rect.y, w, h)


def _strip_aria_state(text: str) -> str:
    return _ARIA_STATE_SUFFIX_RE.sub("", text).strip()


def node_option_text(node: Any) -> str:
    """The human option label for an OPTION node.

    Option text frequently lives in `aria-label` (salesforce: aria-label=
    "PostgreSQL not checked"), not a direct text leaf. Prefer the ax accessible
    name / aria-label, strip trailing a11y-state suffixes, else fall back to the
    element's meaningful text. Committed-pill state suffixes ("press delete to
    clear value") are stripped here; excluding pills entirely is the matcher's job.
    """
    attrs = getattr(node, "attributes", None) or {}
    ax = getattr(node, "ax_node", None)
    candidates: list[str] = []
    if ax is not None and getattr(ax, "name", None):
        candidates.append(ax.name)
    if attrs.get("aria-label"):
        candidates.append(attrs["aria-label"])
    for c in candidates:
        stripped = _strip_aria_state(c)
        if stripped:
            return stripped
    # Fallback: browser-use's own "what the LLM sees" text extraction.
    try:
        txt = node.get_meaningful_text_for_llm()
    except Exception:
        txt = getattr(node, "node_value", "") or ""
    return _strip_aria_state(txt)


def node_label_text(node: Any) -> str:
    """The VISIBLE label a human would read for a form control.

    Human-style perception: we read the rendered accessible name (ax_node.name —
    browser-use already resolves <label for>, aria-labelledby, wrapping <label>),
    then aria-label / placeholder / title, never a renameable class/data-* hook.
    """
    ax = getattr(node, "ax_node", None)
    if ax is not None and getattr(ax, "name", None):
        return ax.name.strip()
    attrs = getattr(node, "attributes", None) or {}
    for attr in ("aria-label", "placeholder", "title", "alt", "name"):
        if attrs.get(attr):
            return attrs[attr].strip()
    try:
        return node.get_meaningful_text_for_llm()
    except Exception:
        return (getattr(node, "node_value", "") or "").strip()


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _overlap_score(label_tokens: set[str], target_tokens: set[str]) -> float:
    """Normalized token overlap (fraction of the wanted label's tokens present)."""
    if not target_tokens:
        return 0.0
    return len(label_tokens & target_tokens) / len(target_tokens)


# Controls a human can fill — everything else is ignored when locating a field.
_FILLABLE_TAGS = {"input", "textarea", "select"}
_FILLABLE_ROLES = {
    "textbox",
    "combobox",
    "radio",
    "checkbox",
    "listbox",
    "spinbutton",
    "switch",
    "searchbox",
}


def _is_fillable_control(node: Any) -> bool:
    attrs = getattr(node, "attributes", None) or {}
    # A file input is NEVER a generic locate candidate. It is reached ONLY via the dedicated
    # file path (_s_file_global, source=="file"); letting it into the generic candidate set lets a
    # non-file field (a location typeahead, a language checkbox) bind to a nearby hidden zero-box
    # input[type=file] and get classified INTRINSIC_FILE -> the value is "uploaded" as a string via
    # setFileInputFiles, which corrupts the SPA's state (observed on Lever element 73 / Ashby
    # element 27 -> browser reset -> wedge). Excluding it here costs nothing: real file fields do
    # not use this ranker at all.
    if _tag(node) == "input" and (attrs.get("type") or "").lower() == "file":
        return False
    if _tag(node) in _FILLABLE_TAGS:
        return True
    if attrs.get("contenteditable") in ("", "true"):
        return True
    role = (attrs.get("role") or "").lower()
    if role in _FILLABLE_ROLES:
        return True
    ax = getattr(node, "ax_node", None)
    return ax is not None and (getattr(ax, "role", None) or "").lower() in _FILLABLE_ROLES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# BUILD FIX (the LAST blocker): the READ path must NOT go through the readiness/serialize-heavy
# event-bus watchdog. ``get_browser_state_summary`` dispatches ``BrowserStateRequestEvent``, whose
# handler (DOMWatchdog.on_BrowserStateRequestEvent) does a stability sleep + tabs + screenshot +
# page_info + popup scan AND shares the bubus EventBus per-handler timeout with the other watchdogs
# (DownloadsWatchdog etc.). On a never-idle Lever/Ashby /apply SPA a TRANSIENT renderer/CDP stall on
# ANY of those sub-steps trips the 30s bus ceiling and ESCALATES every field after the first
# (runs/oa_live/lever2.log). The WRITE path was already fixed by going DIRECT-CDP (oa_cdp_action);
# this is the READ equivalent: call ``DomService.get_serialized_dom_tree`` DIRECTLY — no event bus,
# no screenshot, no network-idle wait, no tabs — wrapped in a HARD ``asyncio.wait_for`` so a single
# wedged CDP round-trip fails FAST and we reuse the last-good snapshot instead of hanging 30s.
DOM_SNAPSHOT_TIMEOUT = float(os.environ.get("OA_DOM_SNAPSHOT_TIMEOUT", "5.0"))

# Per-session last-good snapshot, so a transient serialize stall reuses the previous DOM rather than
# returning an empty map (which would lose every located field). Keyed by id(session) (the live
# BrowserSession is a stable object for the run); cleared implicitly when the session is GC'd.
_LAST_GOOD: dict[int, OAState] = {}
# Session ids that have produced >=1 direct serialize — i.e. REAL BrowserSessions. Used so the read-fail
# fallback never re-dispatches the 30s-hanging event-bus summary on a real session (even post-reset when
# agent_focus_target_id is cleared), while offline fakes (never direct-capable) still use the event bus.
_DIRECT_CAPABLE: set[int] = set()


async def _serialize_direct(session: Any, previous: OAState | None) -> OAState | None:
    """Serialize the page via ``DomService.get_serialized_dom_tree`` DIRECTLY, bypassing the event bus.

    Returns an OAState, or None when this session has no direct DomService path (the offline fakes,
    which only implement ``get_browser_state_summary`` — they take the fallback). Wrapped by the
    caller in ``asyncio.wait_for`` so it can never hang the field loop.

    REAL browser-use API used (verified, file:line):
      * ``session._dom_watchdog`` -> ``DOMWatchdog`` (browser/session.py:1706); its cached
        ``_dom_service`` (watchdog dom_watchdog.py:43/542) keeps the per-target CDP session warm.
      * ``DomService(browser_session=…, …)`` (dom/service.py:46) when no watchdog service exists yet.
      * ``DomService.get_serialized_dom_tree(previous_cached_state=…) -> (SerializedDOMState,
        EnhancedDOMTreeNode, timing_info)`` (dom/service.py:1004). The serializer computes ``is_new``
        by diffing against ``previous_cached_state`` (serializer.py:712-723) — the SAME delta signal
        we already reuse — so passing the last snapshot's ``dom_state`` preserves it for free.
      * ``session.update_cached_selector_map(selector_map)`` (session.py:2368) keeps the rest of
        browser-use consistent (the write watchdog reads the cached map for occluded-node clicks).
      * url/title via ``get_current_page_url`` / ``get_current_page_title`` (session.py:2320/2327) —
        these read ``session_manager.get_target(...)`` SYNCHRONOUSLY (no event bus, no network).
    """
    watchdog = getattr(session, "_dom_watchdog", None)
    # Need a live target to serialize; the direct DomService asserts agent_focus_target_id is set.
    if getattr(session, "agent_focus_target_id", None) is None:
        return None

    dom_service = getattr(watchdog, "_dom_service", None) if watchdog is not None else None
    if dom_service is None:
        # No watchdog service yet (first read) — construct one bound to this session, mirroring the
        # watchdog's own construction (dom_watchdog.py:542-550) so paint-order/iframe knobs match.
        try:
            from browser_use.dom.service import DomService
        except Exception:
            return None
        profile = getattr(session, "browser_profile", None)
        dom_service = DomService(
            browser_session=session,
            cross_origin_iframes=getattr(profile, "cross_origin_iframes", False),
            paint_order_filtering=getattr(profile, "paint_order_filtering", True),
            max_iframes=getattr(profile, "max_iframes", 100),
            max_iframe_depth=getattr(profile, "max_iframe_depth", 5),
        )
        if watchdog is not None:
            # cache it on the watchdog so subsequent reads reuse the warm service (and the watchdog's
            # own state stays coherent if a full get_browser_state_summary is ever taken later).
            with contextlib.suppress(Exception):
                watchdog._dom_service = dom_service

    prev_dom_state = previous.dom_state if previous is not None else None
    serialized, _enhanced, _timing = await dom_service.get_serialized_dom_tree(previous_cached_state=prev_dom_state)
    selector_map = dict(serialized.selector_map) if serialized else {}

    # keep browser-use's cached selector map current (occluded-click fallback reads it).
    with contextlib.suppress(Exception):
        session.update_cached_selector_map(serialized.selector_map if serialized else {})

    url = ""
    title = ""
    with contextlib.suppress(Exception):
        url = await session.get_current_page_url() or ""
    with contextlib.suppress(Exception):
        title = await session.get_current_page_title() or ""

    return OAState(selector_map=selector_map, url=url, title=title, raw=serialized, dom_state=serialized)


async def get_state(session: Any, *, include_screenshot: bool = False, previous: OAState | None = None) -> OAState:
    """Snapshot the current page — FAST + BOUNDED, never the 30s event-bus hang.

    Primary path: ``DomService.get_serialized_dom_tree`` DIRECT (no event bus, no screenshot, no
    network-idle wait), under a hard ``DOM_SNAPSHOT_TIMEOUT``. On timeout/error we reuse the last-good
    snapshot for this session (so a transient stall never wipes the located fields). The OAState /
    selector_map / delta contract is IDENTICAL to before — ``delta(before, after)`` still diffs the
    same ``selector_map`` keys.

    Fallback path (offline fakes / a session with no direct DomService): the original
    ``get_browser_state_summary`` read. ``include_screenshot`` is accepted for signature compatibility
    but the direct path never captures one (the marks tier takes its own screenshot when needed).

    ``previous`` (the last OAState) is forwarded to the serializer as ``previous_cached_state`` so its
    own ``is_new`` delta signal is preserved across reads — passing it is free and keeps the diff
    robust to React re-render churn.
    """
    # DIAGNOSTIC-ONLY get_state timing (default OFF): when OA_GETSTATE_LOG is set, append one line per
    # call recording elapsed seconds + outcome. Pure instrumentation — zero effect on fill behavior.
    _gs_log = os.environ.get("OA_GETSTATE_LOG")
    _gs_t0 = __import__("time").monotonic() if _gs_log else 0.0

    prev = previous if previous is not None else _LAST_GOOD.get(id(session))

    direct = None
    timed_out = False
    try:
        # asyncio.TimeoutError (== builtin TimeoutError) on a wedged serialize, plus any CDP/socket
        # error, all degrade to the last-good-snapshot reuse below — never a 30s hang.
        direct = await asyncio.wait_for(_serialize_direct(session, prev), timeout=DOM_SNAPSHOT_TIMEOUT)
    except TimeoutError:
        timed_out = True
    except Exception:
        direct = None
    if direct is not None:
        _LAST_GOOD[id(session)] = direct
        _DIRECT_CAPABLE.add(id(session))  # mark this as a REAL session (gates the read-fail fallback)
        _gs_record(_gs_log, _gs_t0, "direct", len(direct.selector_map))
        return direct

    # Direct serialize failed. Reuse the last-good snapshot for any session that has EVER produced one
    # via the direct path (a real BrowserSession — marked in _DIRECT_CAPABLE on first success). This
    # covers defect B: after a browser RESET ``agent_focus_target_id`` is cleared so the live ``has_direct``
    # check goes False, but the session is still a real one whose event-bus summary HANGS 30s — so we must
    # NOT fall through to it. Offline fakes never direct-succeed, so they are not in the set and correctly
    # use the event-bus read below (which returns their live map).
    if id(session) in _DIRECT_CAPABLE:
        last = _LAST_GOOD.get(id(session))
        if last is not None:
            _gs_record(_gs_log, _gs_t0, "timeout-reuse" if timed_out else "reuse", len(last.selector_map))
            return last
        _gs_record(_gs_log, _gs_t0, "empty", 0)  # real session, first read failed, no history -> bounded empty (never the 30s bus)
        return OAState(selector_map={})

    # Offline fakes only (never direct-capable) — original event-bus read. If even that fails and we
    # later HAVE a last-good snapshot, reuse it rather than hang/empty.
    try:
        summary = await session.get_browser_state_summary(include_screenshot=include_screenshot, cached=False)
    except Exception:
        last = _LAST_GOOD.get(id(session))
        if last is not None:
            return last
        raise
    dom_state = summary.dom_state
    selector_map = dict(dom_state.selector_map) if dom_state else {}
    state = OAState(
        selector_map=selector_map,
        url=getattr(summary, "url", "") or "",
        title=getattr(summary, "title", "") or "",
        raw=summary,
        dom_state=dom_state,
    )
    _LAST_GOOD[id(session)] = state
    _gs_record(_gs_log, _gs_t0, "summary", len(selector_map))
    return state


def _gs_record(log_path: str | None, t0: float, kind: str, n: int) -> None:
    """DIAGNOSTIC-ONLY: append one get_state-timing line. No-op unless OA_GETSTATE_LOG is set."""
    if not log_path:
        return
    import time as _t

    with contextlib.suppress(Exception), open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{_t.monotonic() - t0:.3f}\t{kind}\t{n}\n")


def locate_field_ranked(state: OAState, label_text: str) -> list[tuple[Any, float]]:
    """Full ranking of visible fillable controls vs `label_text`, best first.

    Lets the state machine detect a label collision (top-score tie => ambiguous,
    forcing a value-verify) without a second pass. Score is normalized token
    overlap in [0, 1]; only positive-overlap controls are returned.
    """
    target = _tokens(label_text)
    scored: list[tuple[Any, float]] = []
    for node in state.selector_map.values():
        if not node_is_visible(node):
            continue
        if not _is_fillable_control(node):
            continue
        score = _overlap_score(_tokens(node_label_text(node)), target)
        if score > 0:
            scored.append((node, score))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Tiered locate — STRUCTURE first, VISUAL PROXIMITY aid, VLM disambiguate.
# The regression root cause: a pure accessible-name ranker (Tier 1 alone) cannot
# reach controls whose visible question is NOT wired to the input (Lever's custom
# question-cards). A human binds them by LOOKING at the text sitting above/left of
# the box. This restores that — generically, with NO per-ATS strings.
# ---------------------------------------------------------------------------

# Minimum Tier-1 accessible-name overlap to accept the structural winner outright.
_STRUCT_STRONG = 0.5
# Two structural candidates within this score are "tied" -> disambiguate, don't guess.
_STRUCT_TIE = 1e-9


def _climb_wrapper(node: Any, *, max_up: int = 4) -> Any:
    """Climb to a small enclosing wrapper that holds this control's QUESTION text.

    A card/question wrapper is the nearest ancestor that contains more than just the input
    (its descendants include the question text node). Bounded climb so we never swallow the
    whole form. Pure DOM-structure (parent_node is populated on the live serializer nodes,
    service.py:810) — no renameable class/data-* hook.
    """
    cur = node
    for _ in range(max_up):
        parent = getattr(cur, "parent_node", None)
        if parent is None:
            break
        cur = parent
        kids = getattr(cur, "children_nodes", None) or []
        # a wrapper that adds siblings/text beyond the bare control is the question group.
        if len(kids) > 1:
            break
    return cur


def _group_text(node: Any) -> str:
    """All human-readable text in the control's question group (the card's question + helper).

    Reuses browser-use's own ``get_all_children_text`` (dom/views.py:561) over the climbed
    wrapper, so we read EXACTLY the text the agent sees — the visible question a label-less
    card never wires to its input. Falls back to the control's own accessible name.
    """
    wrapper = _climb_wrapper(node)
    txt = ""
    getter = getattr(wrapper, "get_all_children_text", None)
    if callable(getter):
        try:
            txt = getter() or ""
        except Exception:
            txt = ""
    if not txt:
        txt = node_label_text(node)
    return txt


# Minimum card-text token-overlap with the question label to accept an ancestor as THE card that
# holds this control's heading. The heading is one node among many in a card's text, so we want a
# solid majority of the (short) label's tokens present — not a single incidental word.
_CARD_TEXT_MATCH = 0.6
# Bounded climb for the card search — a card/section is a few levels up from the bare control, never
# the whole form. Larger than _climb_wrapper's 4 because a grouped control (radio inside a <label>
# inside a fieldset inside the question card) sits deeper below its heading than a bare text input.
_CARD_MAX_UP = 8


def _all_children_text(node: Any) -> str:
    """browser-use's own ``get_all_children_text`` over ``node`` (dom/views.py:561), or ''.
    Reads EXACTLY the visible text the agent sees in this subtree (the card heading + helper +
    option labels), so a heading never wired to its control is still readable by structure."""
    getter = getattr(node, "get_all_children_text", None)
    if not callable(getter):
        return ""
    try:
        return getter() or ""
    except Exception:
        return ""


def _card_wrapper(node: Any, target: set[str], *, max_up: int = _CARD_MAX_UP) -> Any | None:
    """Climb to the nearest ancestor CARD whose visible text contains the question heading.

    A label-less card (Lever screening question) puts its question text in a separate HEADING node
    ABOVE the control — not in the control's own accessible name, and (for a grouped radio/checkbox)
    not even in the small wrapper ``_climb_wrapper`` stops at (that is just the ``<label>Yes</label>``
    row). We climb further, bounded, and accept the FIRST ancestor whose ``get_all_children_text``
    token-overlap with the wanted label clears ``_CARD_TEXT_MATCH`` — i.e. the section that actually
    renders this question's heading. Pure DOM structure + the agent's own text (no class/data-* hook).
    Returns that card node, or None if no ancestor within the bound names the field.
    """
    if not target:
        return None
    cur = node
    for _ in range(max_up):
        parent = getattr(cur, "parent_node", None)
        if parent is None:
            break
        cur = parent
        if _overlap_score(_tokens(_all_children_text(cur)), target) >= _CARD_TEXT_MATCH:
            return cur
    return None


def _controls_in(card: Any) -> list[Any]:
    """Every fillable control in this card's subtree, in document order (DFS).
    Generic structural walk over ``children_nodes`` — the same tree ``get_all_children_text`` reads."""
    out: list[Any] = []

    def walk(n: Any) -> None:
        kids = getattr(n, "children_nodes", None) or []
        for k in kids:
            if _is_fillable_control(k):
                out.append(k)
            walk(k)

    walk(card)
    return out


def locate_grouped_widget(state: OAState, label_text: str) -> tuple[Any, Any] | None:
    """Bind a QUESTION HEADING to its non-text card control(s) by structure + spatial proximity.

    The gap (proven on Lever): a radio/checkbox/single_select/textarea whose question is a separate
    card HEADING is missed by Tier-1 (the control's own name is 'Yes'/'No'/empty) AND by the shallow
    Tier-2 ``_group_text`` (which only reaches the bare option row). This finds the control's enclosing
    CARD (``_card_wrapper`` — the ancestor whose visible text contains the heading), then returns a
    REPRESENTATIVE fillable control inside that card: the topmost-then-leftmost one at/below the
    heading region. ``classify_intrinsic`` on that node then routes radio/checkbox -> ``_s_choice``,
    select -> ``_s_native``, textarea -> ``_s_text`` through the engine's EXISTING fill paths.

    Returns ``(control_node, card_node)`` — the card is handed to the choice-group reader so a radio
    group is scoped to THIS question, not the whole page. None if no card names the field. GENERIC —
    no per-ATS strings, binds purely by the heading text + box geometry.
    """
    target = _tokens(label_text)
    if not target:
        return None
    best: tuple[float, Any, Any] | None = None  # (card_text_score, control, card)
    seen_cards: set[int] = set()
    for ctrl in state.selector_map.values():
        if not node_is_visible(ctrl) or not _is_fillable_control(ctrl):
            continue
        card = _card_wrapper(ctrl, target)
        if card is None:
            continue
        cid = getattr(card, "backend_node_id", None)
        if cid in seen_cards:
            continue
        seen_cards.add(cid)
        score = _overlap_score(_tokens(_all_children_text(card)), target)
        rep = _representative_control(card)
        if rep is None:
            continue
        if best is None or score > best[0]:
            best = (score, rep, card)
    if best is not None:
        return (best[1], best[2])
    # BUTTON-PILL pass (notion/ashby mega/45-46): the question's options are literal <button>s —
    # not fillable controls — so the scan above sees NOTHING and locate reported no-control; the
    # pills stayed empty on every notion/airwallex form. Find the card whose text names the
    # question and which holds 2-6 SHORT-text buttons (the Yes/No pills); return (pill, card) so
    # the choice lane's cdp_choose_option (button-aware) commits scoped to that card.
    _dbg_btns = _dbg_short = _dbg_cards = 0
    for node in state.selector_map.values():
        if not node_is_visible(node):
            continue
        attrs = getattr(node, "attributes", None) or {}
        if _tag(node) != "button" and (attrs.get("role") or "").lower() != "button":
            continue
        _dbg_btns += 1
        txt = _all_children_text(node).strip()
        if not txt or len(txt) > 30:
            continue
        _dbg_short += 1
        card = _card_wrapper(node, target)
        if card is None:
            continue
        _dbg_cards += 1
        pills = [
            n for n in state.selector_map.values()
            if node_is_visible(n)
            and (_tag(n) == "button" or ((getattr(n, "attributes", None) or {}).get("role") or "").lower() == "button")
            and _is_descendant(n, card)
            and 0 < len(_all_children_text(n).strip()) <= 30
        ]
        if 2 <= len(pills) <= 6:
            return (node, card)
    # DIAGNOSTIC (openai mega/48: pill pass live but still no-control — which stage starved?):
    print(f"   [locate] pill-pass miss '{label_text[:40]}': btns={_dbg_btns} short={_dbg_short} carded={_dbg_cards}")
    return None


def _is_descendant(node: Any, ancestor: Any) -> bool:
    """Structural containment via parent walk (bounded)."""
    aid = getattr(ancestor, "backend_node_id", None)
    p = node
    for _ in range(12):
        if p is None:
            return False
        if getattr(p, "backend_node_id", None) == aid:
            return True
        p = getattr(p, "parent_node", None) or getattr(p, "parent", None)
    return False


def _representative_control(card: Any) -> Any | None:
    """The control to bind for a card: topmost-then-leftmost-then-smallest fillable control inside it.
    For a radio/checkbox group this is the FIRST option (its intrinsic kind is what ``classify_intrinsic``
    routes on); for a single textarea/select it is that control. Geometry-only on absolute_position."""
    controls = _controls_in(card)
    if not controls:
        return None
    best: Any = None
    best_key: tuple[float, float, float] | None = None
    for n in controls:
        rect = node_rect(n)
        y = rect[1] if rect else float("inf")
        x = rect[0] if rect else float("inf")
        area = (rect[2] * rect[3]) if rect else float("inf")
        key = (y, x, area)
        if best_key is None or key < best_key:
            best_key, best = key, n
    return best


def locate_field(state: OAState, label_text: str) -> EnhancedDOMTreeNode | None:
    """Find the control whose VISIBLE label best matches `label_text` (structure-only, legacy).

    Human-style: rank visible, fillable controls by token-overlap of their rendered
    label (ax accessible name / aria-label / placeholder) against the wanted label.
    Returns the single best node, or None if nothing visible matches at all.

    NOTE: this is Tier-1 only. The state machine calls ``locate_field_tiered`` for the full
    structure + spatial + VLM cascade; this remains for the choice-group reader / legacy tests.
    """
    ranked = locate_field_ranked(state, label_text)
    if not ranked:
        return None
    return ranked[0][0]


def _group_container(node: Any, *, max_up: int = _CARD_MAX_UP) -> Any | None:
    """Climb from a control to the smallest ancestor whose subtree holds >1 control of THIS control's
    intrinsic kind — the radio/checkbox GROUP container. Used to scope a choice group when a card was
    bound VISUALLY (set-of-marks) and so no heading-text card is known: the choice-group reader must
    still pick from THIS question's options, not the whole page. Returns that ancestor, or None when
    the control is a lone single-value widget (a textarea/select — no group to scope). Pure DOM
    structure on ``parent_node`` / ``children_nodes`` — no renameable hook."""
    from oa_observe_act import classify_intrinsic  # local import: observe_act imports perception

    want_kind = classify_intrinsic(node)
    if want_kind not in ("INTRINSIC_RADIO", "INTRINSIC_CHECKBOX"):
        return None  # only choice groups need a scoped container; a select/textarea is self-scoped
    cur = node
    for _ in range(max_up):
        parent = getattr(cur, "parent_node", None)
        if parent is None:
            break
        cur = parent
        same = [c for c in _controls_in(cur) if classify_intrinsic(c) == want_kind]
        if len(same) > 1:
            return cur
    return None


_DOM_REF_ATTRS = ("id", "name", "data-ui", "data-testid", "data-automation-id", "data-fkit-id")


def _locate_by_dom_ref(state: OAState, ref: str) -> Any | None:
    """Re-find the element discovery enumerated, by identity-attribute EQUALITY (no semantics,
    no similarity). Multiple elements can share a ref (id vs a hidden combined input's name —
    the live workable phone case): prefer an id match, then a visible fillable control, so we
    bind the element the USER fills, not a shadow value-carrier. Returns None when the ref isn't
    in the current selector_map (below fold, re-render) — the caller falls through to the ranked
    tiers unchanged."""
    if not ref:
        return None
    matches: list[tuple[int, Any]] = []
    for n in state.selector_map.values():
        a = getattr(n, "attributes", None) or {}
        if not any(a.get(k) == ref for k in _DOM_REF_ATTRS):
            continue
        score = (2 if a.get("id") == ref else 0) + (1 if node_is_visible(n) and _is_fillable_control(n) else 0)
        matches.append((score, n))
    if not matches:
        return None
    return max(matches, key=lambda t: t[0])[1]


async def locate_field_tiered(
    state: OAState,
    label_text: str,
    *,
    vlm_pick: Any = None,
    marks_pick: Any = None,
    dom_ref: str = "",
) -> tuple[Any, str, Any]:
    """Locate the control for `label_text` by STRUCTURE first, VISUAL PROXIMITY aid, then VLM.

    Returns ``(node, how, card)`` with ``how`` in {"dom-ref", "structure", "spatial", "grouped",
    "vlm", "marks"},
    or ``(None, "", None)`` when nothing plausible exists. ``card`` is the enclosing question-card node
    when the bind came from the grouped-widget OR the visual set-of-marks tier (so the choice-group
    reader can scope a radio group to THIS question), else None. GENERIC — no per-ATS code.

      TIER 1 STRUCTURE: rank visible fillable controls by accessible-name token overlap
        (``locate_field_ranked``; ax_node.name already resolves <label for>/aria-labelledby/
        wrapping-label). A clear strong winner -> return ("structure").
      TIER 2 SPATIAL: a single control whose own shallow question-group text (``_group_text``) names
        the field — the proven label-less TEXT-input case (Lever 'Preferred Name'). Returns ("spatial").
      TIER 2b GROUPED-WIDGET: when no single control's shallow text matches (a radio/checkbox/select/
        textarea whose question is a separate card HEADING above it — the control's own text is just
        'Yes'/'No'/empty), bind via ``locate_grouped_widget``: find the enclosing CARD whose visible
        text contains the heading, return a representative control inside it. Returns ("grouped", card).
      TIER 3 VLM: only when >=2 candidates tie spatially, ask the optional ``vlm_pick`` callback
        (an async (label, [nodes]) -> node | None) to disambiguate. AID only, bounded, never primary.
      TIER 2d VISUAL SET-OF-MARKS: the LAST resort when STRUCTURE + spatial + grouped-text all miss
        a non-text card (a heading sharing NO tokens with any control — a rich/imaged heading). Mark
        the candidate controls on the screenshot (browser-use ``create_highlighted_screenshot``,
        numbered by backend_node_id) and ask the optional ``marks_pick`` callback (an async
        (label, [nodes]) -> node | None) which marked control is the answer for this question. Binds
        a card the way a HUMAN SEES it, no label. Returns ("marks", card) — the card is the picked
        control's choice-group container (scopes a radio group), or None for a lone textarea/select.
    """
    target = _tokens(label_text)
    if not target:
        return (None, "", None)

    # ---- TIER 0: DOM-REF — discovery read this control's own id/name off the live DOM; re-find
    # THAT element. Identity beats similarity: rating questions differing only at the label tail
    # tie in the token ranker and fell to spatial, which bound a NEIGHBOUR's widget (live: one
    # rating committed into the phone country list; another typed '4' into an essay). ----
    if dom_ref:
        hit = _locate_by_dom_ref(state, dom_ref)
        if hit is not None:
            return (hit, "dom-ref", None)

    # ---- TIER 1: structure (accessible name) ----
    ranked = locate_field_ranked(state, label_text)
    if ranked:
        top_node, top_score = ranked[0]
        tied = len(ranked) >= 2 and abs(ranked[0][1] - ranked[1][1]) < _STRUCT_TIE
        if top_score >= _STRUCT_STRONG and not tied:
            return (top_node, "structure", None)

    # ---- TIER 2: visual proximity (shallow question text near a single control) ----
    controls = [n for n in state.selector_map.values() if node_is_visible(n) and _is_fillable_control(n)]
    text_scored: list[tuple[Any, float, str]] = []
    for n in controls:
        gt = _group_text(n)
        s = _overlap_score(_tokens(gt), target)
        # CONTAINMENT GATE (mega census: spatial JUNK rate 26% vs dom-ref 1.7%): a candidate
        # qualifies only when its OWN question-group text carries at least half the wanted
        # question's tokens — the same bar as _STRUCT_STRONG. `s > 0` let a single shared
        # token ('you', 'work') nominate a NEIGHBOUR's control, and the always-an-answer
        # fallback then bound it (agility mega/24: a yes/no question bound the Phone input,
        # Phone became 'Yes'; hibob mega/19-21: Country bound First name). No strong
        # candidate -> fall through to grouped / honest NOT-FOUND — escalate beats wrong-fill.
        if s >= _STRUCT_STRONG:
            text_scored.append((n, s, gt))
    if text_scored:
        text_scored.sort(key=lambda t: t[1], reverse=True)
        best_s = text_scored[0][1]
        # candidates whose question-group text matches the label about equally well.
        cands = [n for (n, s, _g) in text_scored if best_s - s < 0.2]
        if len(cands) == 1:
            return (cands[0], "spatial", None)
        # >=2 group-text matches: prefer the geometric "answer directly below the question".
        geo = _disambiguate_spatial(state, target, cands)
        if geo is not None:
            return (geo, "spatial", None)
        # ---- TIER 3: VLM aid for a genuine spatial tie ----
        if vlm_pick is not None:
            picked = None
            with contextlib.suppress(Exception):
                picked = await vlm_pick(label_text, cands)
            if picked is not None:
                return (picked, "vlm", None)
        return (cands[0], "spatial", None)  # bounded fallback: best text match

    # ---- TIER 2b: GROUPED WIDGET — a card heading binds its non-text control(s) ----
    # The shallow ``_group_text`` only reaches the bare option row of a radio/checkbox (text 'Yes'),
    # never the card heading; and a textarea/select card's heading is a separate node above it. Climb
    # to the enclosing card whose visible text holds the heading and bind a representative control.
    grouped = locate_grouped_widget(state, label_text)
    if grouped is not None:
        node, card = grouped
        return (node, "grouped", card)

    # ---- TIER 2d: VISUAL SET-OF-MARKS — bind a label-free card the way a human SEES it ----
    # Structure + spatial + grouped-text all failed to NAME this control (its heading shares no
    # tokens with any control — e.g. an imaged/rich heading, an icon radio group). Mark the
    # candidate non-text controls on the screenshot and let the VLM pick which one is THIS question.
    if marks_pick is not None:
        marks_cands = _marks_candidates(state)
        if marks_cands:
            picked = None
            with contextlib.suppress(Exception):
                picked = await marks_pick(label_text, marks_cands)
            if picked is not None:
                card = _group_container(picked)  # scope a choice group to this question; None if lone
                return (picked, "marks", card)

    # Tier 1 had only a weak match but nothing else bound -> take the weak structural node.
    if ranked:
        return (ranked[0][0], "structure", None)
    return (None, "", None)


def _marks_candidates(state: OAState) -> list[Any]:
    """The non-text fillable controls to offer the visual set-of-marks pick: radio/checkbox/select/
    textarea (and combobox), with a real on-page box. We EXCLUDE plain single-line text inputs — a
    label-free card that defeats structure+spatial+grouped is the non-text widget case the marks tier
    exists for, and including every text box would bloat the marks (and risk a wrong bind). One
    representative per radio/checkbox group is not needed: the VLM picks any option, and the choice-
    group reader re-scopes from it. Returns visible, boxed candidates in document order."""
    out: list[Any] = []
    for n in state.selector_map.values():
        if not node_is_visible(n) or not _is_fillable_control(n):
            continue
        if node_rect(n) is None:
            continue
        tag = _tag(n)
        attrs = getattr(n, "attributes", None) or {}
        typ = (attrs.get("type") or "").lower()
        role = (attrs.get("role") or "").lower()
        ax = getattr(n, "ax_node", None)
        ax_role = ((getattr(ax, "role", None) or "") if ax else "").lower()
        is_plain_text = (
            tag == "input"
            and typ in ("", "text", "email", "url", "tel", "search")
            and role
            not in (
                "combobox",
                "radio",
                "checkbox",
            )
        )
        if is_plain_text and ax_role not in ("combobox",):
            continue  # skip bare text inputs — the marks tier is for non-text card widgets
        out.append(n)
    return out


def _disambiguate_spatial(state: OAState, target: set[str], cands: list[Any]) -> Any | None:
    """Among tied text-match controls, pick the one whose box sits closest BELOW the question
    text region that names this field. Geometry-only, on absolute_position boxes. None if no
    clear nearest-below winner (caller falls to VLM / best-text)."""
    # The question region = the candidate-group wrapper whose text best names the field; use the
    # control rects directly: pick the topmost control among the tied group whose own group text
    # most specifically matches, breaking ties by smallest box (a single input, not a container).
    best: Any = None
    best_key: tuple[float, float] | None = None
    for n in cands:
        rect = node_rect(n)
        if rect is None:
            continue
        _x, y, w, h = rect
        key = (y, w * h)  # higher on the page first, then the smaller (more specific) box
        if best_key is None or key < best_key:
            best_key, best = key, n
    return best


def delta(before: OAState, after: OAState) -> list[DeltaNode]:
    """The nodes present in `after` but not `before` — the option-cluster that appeared.

    This reuses browser-use's OWN delta signal: the serializer flags `is_new` by
    diffing the current backend_node_id set against the previous selector_map
    (serializer.py:712-723). We compute the identical diff over `selector_map` keys
    (backend_node_id is stable per live DOM node), so a click/keystroke that mounts a
    portal menu surfaces exactly its new option nodes. Returned visible-first, with
    each node's option text + document-space center coords for downstream hit-testing.
    """
    before_ids = set(before.selector_map.keys())
    out: list[DeltaNode] = []
    for bnid, node in after.selector_map.items():
        if bnid in before_ids:
            continue
        out.append(
            DeltaNode(
                backend_node_id=bnid,
                node=node,
                text=node_option_text(node),
                center=node_center(node),
            )
        )
    # Visible nodes first, then by vertical position (an option column reads top-down).
    out.sort(
        key=lambda d: (
            not node_is_visible(d.node),
            d.center[1] if d.center else float("inf"),
        )
    )
    return out


# ---------------------------------------------------------------------------
# Offline self-test — exercises the diff logic on two synthetic selector_maps.
# Builds REAL EnhancedDOMTreeNode instances so the accessors are tested against
# the actual browser-use type, not a stand-in. No browser, no network.
# ---------------------------------------------------------------------------


def _make_node(
    backend_node_id: int,
    *,
    tag: str = "div",
    role: str | None = None,
    ax_name: str | None = None,
    attributes: dict[str, str] | None = None,
    visible: bool = True,
    box: tuple[float, float, float, float] | None = None,
) -> Any:
    """Construct a minimal-but-real EnhancedDOMTreeNode for offline tests."""
    from browser_use.dom.views import (
        DOMRect,
        EnhancedAXNode,
        EnhancedDOMTreeNode,
        NodeType,
    )

    ax = None
    if role is not None or ax_name is not None:
        ax = EnhancedAXNode(
            ax_node_id=f"ax{backend_node_id}",
            ignored=False,
            role=role,
            name=ax_name,
            description=None,
            properties=None,
            child_ids=None,
        )
    rect = DOMRect(x=box[0], y=box[1], width=box[2], height=box[3]) if box else None
    return EnhancedDOMTreeNode(
        node_id=backend_node_id,
        backend_node_id=backend_node_id,
        node_type=NodeType.ELEMENT_NODE,
        node_name=tag.upper(),
        node_value="",
        attributes=attributes or {},
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


def _selftest() -> None:
    # BEFORE: a closed "Degree" combobox + an unrelated "School" textbox.
    degree = _make_node(1, tag="input", role="combobox", ax_name="Degree", box=(100, 200, 220, 32))
    school = _make_node(2, tag="input", role="combobox", ax_name="School", box=(100, 120, 220, 32))
    before = OAState(selector_map={1: degree, 2: school}, url="https://x/app")

    # locate by VISIBLE label meaning, not by any attribute.
    assert locate_field(before, "Degree") is degree, "locate_field Degree"
    assert locate_field(before, "School or University") is school, "locate_field School"
    assert locate_field(before, "Salary expectation") is None, "no spurious match"

    # AFTER a click on Degree: three option nodes mounted (new backend_node_ids),
    # plus the same two controls (unchanged keys). Option text lives in aria-label
    # with a trailing a11y-state suffix, like the salesforce capture.
    opt_b = _make_node(
        10,
        tag="div",
        role="option",
        attributes={"aria-label": "Bachelor's not checked"},
        box=(100, 234, 220, 28),
    )
    opt_m = _make_node(
        11,
        tag="div",
        role="option",
        attributes={"aria-label": "Master's not checked"},
        box=(100, 262, 220, 28),
    )
    opt_p = _make_node(
        12,
        tag="div",
        role="option",
        attributes={"aria-label": "PhD not checked"},
        box=(100, 290, 220, 28),
    )
    after = OAState(
        selector_map={1: degree, 2: school, 10: opt_b, 11: opt_m, 12: opt_p},
        url="https://x/app",
    )

    d = delta(before, after)
    ids = [dn.backend_node_id for dn in d]
    assert ids == [10, 11, 12], f"delta ids wrong/unsorted: {ids}"
    texts = [dn.text for dn in d]
    assert texts == ["Bachelor's", "Master's", "PhD"], f"option text wrong: {texts}"
    # centers are document-space and correctly ordered top-to-bottom.
    centers = [dn.center for dn in d]
    assert centers[0] == (210.0, 248.0), f"center wrong: {centers[0]}"
    assert all(c is not None for c in centers), "every option has a box"
    # unchanged controls must NOT appear in the delta.
    assert 1 not in ids and 2 not in ids, "stale controls leaked into delta"

    # No-delta case (re-read with identical map) yields nothing.
    assert delta(before, before) == [], "empty delta on identical states"

    # A hidden new node sorts AFTER visible ones.
    hidden_opt = _make_node(
        20,
        tag="div",
        role="option",
        attributes={"aria-label": "Other"},
        visible=False,
        box=(100, 100, 50, 20),
    )
    after2 = OAState(selector_map={**after.selector_map, 20: hidden_opt}, url="https://x/app")
    d2 = delta(before, after2)
    assert d2[-1].backend_node_id == 20, "hidden node should sort last"

    # ---- GROUPED-WIDGET locate (the gap fix): a card heading binds its non-text controls. ----
    # Built with the same fakes the state-machine tests use (local import — fakes imports this module,
    # so the import lives inside the function to avoid an import-time cycle).
    from oa_observe_act_fakes import make_choice_card, make_single_input_card

    # Two radio cards on one page (authorize + sponsorship) — the page-wide grouping hazard.
    auth = make_choice_card("Are you legally authorized to work?", ["Yes", "No"], base_bnid=800, kind="radio", top=200)
    spon = make_choice_card("Will you require visa sponsorship?", ["Yes", "No"], base_bnid=820, kind="radio", top=400)
    gstate = OAState(selector_map={n.backend_node_id: n for n in (auth + spon)}, url="https://x/apply")

    # Tier-1 structure cannot bind the heading: the only matches are radios whose own name is
    # 'Yes'/'No', which share NO tokens with the question -> the ranker returns nothing.
    assert locate_field_ranked(gstate, "Will you require visa sponsorship?") == [], "structure must miss the heading"
    g = locate_grouped_widget(gstate, "Will you require visa sponsorship?")
    assert g is not None, "grouped-widget must bind the sponsorship card"
    rep, card = g
    # the representative is a radio IN the sponsorship card (one of spon), NEVER the authorize card.
    spon_ids = {n.backend_node_id for n in spon}
    auth_ids = {n.backend_node_id for n in auth}
    assert rep.backend_node_id in spon_ids, f"bound rep not in sponsorship card: {rep.backend_node_id}"
    # the card subtree contains ONLY this question's controls (scoping) — no authorize radios.
    scoped = {n.backend_node_id for n in _controls_in(card)}
    assert scoped <= spon_ids and not (scoped & auth_ids), f"choice group not scoped: {scoped}"
    assert scoped == spon_ids, f"scoped group must be exactly the sponsorship radios: {scoped}"

    # A single-control card (textarea nested in its own field-wrapper, heading a sibling block) binds.
    ta_card = make_single_input_card("What is your proudest accomplishment?", bnid=860, tag="textarea", top=600)
    tstate = OAState(selector_map={ta_card.backend_node_id: ta_card}, url="https://x/apply")
    gt = locate_grouped_widget(tstate, "What is your proudest accomplishment?")
    assert gt is not None and gt[0].backend_node_id == ta_card.backend_node_id, "textarea card must bind"

    # NEGATIVE: a label naming no card on the page binds nothing (no false positive).
    assert locate_grouped_widget(gstate, "What is your favorite color?") is None, "no spurious grouped bind"

    # ---- BUILD FIX C: VISUAL SET-OF-MARKS tier (Tier-2d) on a LABEL-FREE card ----
    # A radio card whose HEADING ("Work eligibility") shares no tokens with the question we search
    # ("Are you authorized to work?"): structure, spatial, AND grouped-text all miss, so the tiered
    # locate falls through to marks_pick. We feed a fake marks_pick that returns the first radio.
    import asyncio as _aio

    lf = make_choice_card("Work eligibility", ["Yes", "No"], base_bnid=900, kind="radio", top=240)
    lfstate = OAState(selector_map={n.backend_node_id: n for n in lf}, url="https://x/apply")
    target_label = "Are you authorized to work?"
    # all heading-text tiers must MISS (no token overlap with the question).
    assert locate_field_ranked(lfstate, target_label) == [], "structure must miss the label-free card"
    assert locate_grouped_widget(lfstate, target_label) is None, "grouped-text must miss the label-free card"

    # marks candidates = the radios (non-text controls), filtered to visible+boxed.
    mc = _marks_candidates(lfstate)
    assert {n.backend_node_id for n in mc} == {n.backend_node_id for n in lf}, "marks candidates = the radios"

    async def _fake_marks_pick(label: str, cands: list[Any]) -> Any:
        return cands[0]  # pick the 'Yes' radio

    node, how, card = _aio.run(locate_field_tiered(lfstate, target_label, marks_pick=_fake_marks_pick))
    assert how == "marks" and node is lf[0], f"marks tier must bind the label-free card: {how}"
    # the marks bind returns a choice-group CARD (the radio group container) so _s_choice scopes to it.
    assert card is not None, "marks bind of a radio group returns a scoping card"
    scoped = {n.backend_node_id for n in _controls_in(card)}
    assert scoped == {n.backend_node_id for n in lf}, f"group container scopes exactly the card radios: {scoped}"

    # marks tier excludes a PLAIN TEXT input (it is for non-text widgets); a lone text box is not offered.
    plain = _make_node(950, tag="input", role="textbox", ax_name="", box=(100, 100, 200, 30))
    assert _marks_candidates(OAState(selector_map={950: plain})) == [], "plain text input excluded from marks"

    # ---- THE LAST-BLOCKER FIX: BOUNDED + DIRECT get_state (no event-bus hang) + snapshot reuse. ----
    _selftest_get_state()

    print(
        "oa_perception self-test OK: locate_field + delta + option-text + coords + grouped-widget + "
        "marks + bounded-direct-get_state"
    )


# --------------------------------------------------------------------------- #
# get_state self-test — proves the READ path: (a) DIRECT DomService serialize bypasses the
# event-bus entirely, (b) a hung serialize is bounded by DOM_SNAPSHOT_TIMEOUT and falls back to the
# last-good snapshot (NEVER the 30s hang), (c) ``previous`` flows to the serializer as
# previous_cached_state (snapshot-reuse delta), (d) a session with no direct path degrades to
# get_browser_state_summary. No browser, no network, $0.
# --------------------------------------------------------------------------- #
def _selftest_get_state() -> None:
    import asyncio as _aio
    import time as _time

    class _FakeSerialized:
        def __init__(self, selector_map: dict[int, Any]) -> None:
            self.selector_map = selector_map

    class _FakeDomService:
        """Records the previous_cached_state it is handed (snapshot-reuse proof) and returns a
        scripted serialized DOM state directly — the DIRECT path, no event bus."""

        def __init__(self, selector_map: dict[int, Any]) -> None:
            self._sm = selector_map
            self.prev_seen: list[Any] = []
            self.calls = 0

        async def get_serialized_dom_tree(self, previous_cached_state: Any = None):
            self.calls += 1
            self.prev_seen.append(previous_cached_state)
            return _FakeSerialized(dict(self._sm)), None, {}

    class _FakeWatchdog:
        def __init__(self, dom_service: Any) -> None:
            self._dom_service = dom_service

    class _DirectSession:
        """A session exposing the DIRECT serialize path: _dom_watchdog._dom_service + a set
        agent_focus_target_id + the synchronous url/title reads + update_cached_selector_map."""

        def __init__(self, dom_service: Any) -> None:
            self._dom_watchdog = _FakeWatchdog(dom_service)
            self.agent_focus_target_id = "tgt-1"
            self.cached_map: Any = None
            self.summary_calls = 0

        def update_cached_selector_map(self, sm: Any) -> None:
            self.cached_map = sm

        async def get_current_page_url(self) -> str:
            return "https://x/apply"

        async def get_current_page_title(self) -> str:
            return "Apply"

        async def get_browser_state_summary(self, *, include_screenshot: bool = False, cached: bool = False) -> Any:
            self.summary_calls += 1
            raise AssertionError("DIRECT path must NOT fall through to get_browser_state_summary")

    async def _run() -> None:
        _LAST_GOOD.clear()
        node = _make_node(1, tag="input", role="textbox", ax_name="First Name", box=(0, 0, 10, 10))
        svc = _FakeDomService({1: node})
        sess = _DirectSession(svc)

        # (a) DIRECT serialize — no event bus, selector_map carried through, cached map updated.
        s1 = await get_state(sess)
        assert s1.selector_map == {1: node}, "direct serialize selector_map"
        assert sess.summary_calls == 0, "must NOT use the event-bus summary path"
        assert sess.cached_map is not None, "update_cached_selector_map called"
        assert svc.prev_seen[0] is None, "first read has no previous_cached_state"

        # (c) snapshot-reuse: passing ``previous`` flows to the serializer as previous_cached_state.
        s2 = await get_state(sess, previous=s1)
        assert svc.prev_seen[1] is s1.dom_state, "previous flows as previous_cached_state"
        # and with NO explicit previous, the per-session last-good is used as previous (free reuse).
        await get_state(sess)
        assert svc.prev_seen[2] is s2.dom_state, "last-good auto-used as previous_cached_state"

    _aio.run(_run())

    # (b) BOUNDED: a serialize that hangs past DOM_SNAPSHOT_TIMEOUT returns the LAST-GOOD snapshot
    #     fast, never the 30s event-bus hang. We shrink the timeout for the test.
    async def _run_hang() -> None:
        global DOM_SNAPSHOT_TIMEOUT
        node = _make_node(2, tag="input", ax_name="Last Name", box=(0, 0, 10, 10))

        class _HangService:
            async def get_serialized_dom_tree(self, previous_cached_state: Any = None):
                await asyncio.sleep(999)  # never settles — the SPA stall

        class _HangSession(_DirectSession):
            async def get_browser_state_summary(self, *, include_screenshot: bool = False, cached: bool = False) -> Any:
                # the fallback should NOT be reached either — a last-good snapshot exists.
                self.summary_calls += 1
                return type("S", (), {"dom_state": type("D", (), {"selector_map": {2: node}})()})()

        _LAST_GOOD.clear()
        good_svc = _FakeDomService({2: node})
        hs = _HangSession(good_svc)
        good = await get_state(hs)  # primes last-good
        # swap in the hanging service; next read must time out -> reuse last-good.
        hs._dom_watchdog._dom_service = _HangService()
        saved = DOM_SNAPSHOT_TIMEOUT
        DOM_SNAPSHOT_TIMEOUT = 0.15
        try:
            t0 = _time.monotonic()
            reused = await get_state(hs)
            elapsed = _time.monotonic() - t0
        finally:
            DOM_SNAPSHOT_TIMEOUT = saved
        assert elapsed < 1.0, f"bounded read must return fast, not 30s: {elapsed:.2f}s"
        assert reused is good, "hung serialize reuses the last-good snapshot"

    _aio.run(_run_hang())

    # (d) NO direct path (no agent_focus_target_id) -> degrade to get_browser_state_summary (the fakes).
    async def _run_fallback() -> None:
        node = _make_node(3, tag="input", ax_name="Email", box=(0, 0, 10, 10))

        class _SummaryOnlySession:
            agent_focus_target_id = None  # no live target -> no direct serialize

            def __init__(self) -> None:
                self.calls = 0

            async def get_browser_state_summary(self, *, include_screenshot: bool = False, cached: bool = False) -> Any:
                self.calls += 1
                return type(
                    "S", (), {"dom_state": type("D", (), {"selector_map": {3: node}})(), "url": "u", "title": "t"}
                )()

        _LAST_GOOD.clear()
        ss = _SummaryOnlySession()
        st = await get_state(ss)
        assert ss.calls == 1 and st.selector_map == {3: node}, "no-direct-path degrades to summary"

    _aio.run(_run_fallback())


if __name__ == "__main__":
    _selftest()
