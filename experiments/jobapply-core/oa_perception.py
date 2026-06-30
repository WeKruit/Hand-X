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
    summary = await session.get_browser_state_summary(
        include_screenshot=include_screenshot, cached=False
    )
    dom_state = summary.dom_state
    selector_map = dict(dom_state.selector_map) if dom_state else {}
    return OAState(
        selector_map=selector_map,
        url=getattr(summary, "url", "") or "",
        title=getattr(summary, "title", "") or "",
        raw=summary,
    )


def locate_field(state: OAState, label_text: str) -> EnhancedDOMTreeNode | None:
    """Find the control whose VISIBLE label best matches `label_text`.

    Human-style: rank visible, fillable controls by token-overlap of their rendered
    label (ax accessible name / aria-label / placeholder) against the wanted label.
    Returns the single best node, or None if nothing visible matches at all.
    Ties / ambiguity are surfaced by `locate_field_ranked`.
    """
    ranked = locate_field_ranked(state, label_text)
    if not ranked:
        return None
    return ranked[0][0]


def locate_field_ranked(
    state: OAState, label_text: str
) -> list[tuple[Any, float]]:
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
    degree = _make_node(
        1, tag="input", role="combobox", ax_name="Degree", box=(100, 200, 220, 32)
    )
    school = _make_node(
        2, tag="input", role="combobox", ax_name="School", box=(100, 120, 220, 32)
    )
    before = OAState(selector_map={1: degree, 2: school}, url="https://x/app")

    # locate by VISIBLE label meaning, not by any attribute.
    assert locate_field(before, "Degree") is degree, "locate_field Degree"
    assert locate_field(before, "School or University") is school, "locate_field School"
    assert locate_field(before, "Salary expectation") is None, "no spurious match"

    # AFTER a click on Degree: three option nodes mounted (new backend_node_ids),
    # plus the same two controls (unchanged keys). Option text lives in aria-label
    # with a trailing a11y-state suffix, like the salesforce capture.
    opt_b = _make_node(
        10, tag="div", role="option", attributes={"aria-label": "Bachelor's not checked"},
        box=(100, 234, 220, 28),
    )
    opt_m = _make_node(
        11, tag="div", role="option", attributes={"aria-label": "Master's not checked"},
        box=(100, 262, 220, 28),
    )
    opt_p = _make_node(
        12, tag="div", role="option", attributes={"aria-label": "PhD not checked"},
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
        20, tag="div", role="option", attributes={"aria-label": "Other"},
        visible=False, box=(100, 100, 50, 20),
    )
    after2 = OAState(
        selector_map={**after.selector_map, 20: hidden_opt}, url="https://x/app"
    )
    d2 = delta(before, after2)
    assert d2[-1].backend_node_id == 20, "hidden node should sort last"

    print("oa_perception self-test OK: locate_field + delta + option-text + coords")


if __name__ == "__main__":
    _selftest()
