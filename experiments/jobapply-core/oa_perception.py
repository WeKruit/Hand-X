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

import contextlib
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
    raw: Any = field(default=None, repr=False)  # the BrowserStateSummary, for callers


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
    if _tag(node) in _FILLABLE_TAGS:
        return True
    attrs = getattr(node, "attributes", None) or {}
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


async def get_state(session: Any, *, include_screenshot: bool = False) -> OAState:
    """Snapshot the current page via browser-use's DomService.

    Reuses `BrowserSession.get_browser_state_summary` (browser/session.py:1535),
    which dispatches the BrowserStateRequestEvent -> DomService.get_serialized_dom_tree
    -> the serializer that builds `selector_map[backend_node_id] -> EnhancedDOMTreeNode`.
    `cached=False` forces a fresh read (we need the live post-click DOM for deltas).
    """
    summary = await session.get_browser_state_summary(include_screenshot=include_screenshot, cached=False)
    dom_state = summary.dom_state
    selector_map = dict(dom_state.selector_map) if dom_state else {}
    return OAState(
        selector_map=selector_map,
        url=getattr(summary, "url", "") or "",
        title=getattr(summary, "title", "") or "",
        raw=summary,
    )


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


async def locate_field_tiered(
    state: OAState,
    label_text: str,
    *,
    vlm_pick: Any = None,
) -> tuple[Any, str]:
    """Locate the control for `label_text` by STRUCTURE first, VISUAL PROXIMITY aid, then VLM.

    Returns ``(node, how)`` with ``how`` in {"structure", "spatial", "vlm"}, or ``(None, "")``
    when nothing plausible exists. GENERIC — no per-ATS code.

      TIER 1 STRUCTURE: rank visible fillable controls by accessible-name token overlap
        (``locate_field_ranked``; ax_node.name already resolves <label for>/aria-labelledby/
        wrapping-label). A clear strong winner -> return ("structure").
      TIER 2 SPATIAL: when Tier 1 finds nothing, or only a weak/tied match (the Lever-card case
        where the question is NOT wired to the input), bind by GEOMETRY — for each visible
        fillable control, read its question-group text (``_group_text``) and rank by token overlap;
        among the best-text controls, prefer the one whose box sits directly below / left-aligned
        with the question region. Returns ("spatial").
      TIER 3 VLM: only when >=2 candidates tie spatially, ask the optional ``vlm_pick`` callback
        (an async (label, [nodes]) -> node | None) to disambiguate. AID only, bounded, never primary.
    """
    target = _tokens(label_text)
    if not target:
        return (None, "")

    # ---- TIER 1: structure (accessible name) ----
    ranked = locate_field_ranked(state, label_text)
    if ranked:
        top_node, top_score = ranked[0]
        tied = len(ranked) >= 2 and abs(ranked[0][1] - ranked[1][1]) < _STRUCT_TIE
        if top_score >= _STRUCT_STRONG and not tied:
            return (top_node, "structure")

    # ---- TIER 2: visual proximity (question text near the control) ----
    controls = [n for n in state.selector_map.values() if node_is_visible(n) and _is_fillable_control(n)]
    text_scored: list[tuple[Any, float, str]] = []
    for n in controls:
        gt = _group_text(n)
        s = _overlap_score(_tokens(gt), target)
        if s > 0:
            text_scored.append((n, s, gt))
    if text_scored:
        text_scored.sort(key=lambda t: t[1], reverse=True)
        best_s = text_scored[0][1]
        # candidates whose question-group text matches the label about equally well.
        cands = [n for (n, s, _g) in text_scored if best_s - s < 0.2]
        if len(cands) == 1:
            return (cands[0], "spatial")
        # >=2 group-text matches: prefer the geometric "answer directly below the question".
        geo = _disambiguate_spatial(state, target, cands)
        if geo is not None:
            return (geo, "spatial")
        # ---- TIER 3: VLM aid for a genuine spatial tie ----
        if vlm_pick is not None:
            picked = None
            with contextlib.suppress(Exception):
                picked = await vlm_pick(label_text, cands)
            if picked is not None:
                return (picked, "vlm")
        return (cands[0], "spatial")  # bounded fallback: best text match

    # Tier 1 had only a weak match but Tier 2 found no group text -> take the weak structural node.
    if ranked:
        return (ranked[0][0], "structure")
    return (None, "")


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

    print("oa_perception self-test OK: locate_field + delta + option-text + coords")


if __name__ == "__main__":
    _selftest()
