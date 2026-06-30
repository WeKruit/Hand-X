"""oa_file_locate — GLOBAL deterministic file-upload locate + already-uploaded read (any ATS).

THE REGRESSION this restores: the generic ``locate_field`` ranks controls by accessible-name
association, but a file ``input[type=file]`` is almost always HIDDEN / zero-box (a styled "Attach"
button proxies for it) — so it has no readable label, no box, and ``locate_field`` returns
``no-control``. The OLD per-ATS adapters never relied on the label: ``ats_greenhouse`` /
``ats_lever`` located the file input DIRECTLY (``input[type=file]``) and uploaded via CDP
``setFileInputFiles`` (NO os dialog). This module is that proven behaviour, made GENERIC:

  * ``find_file_input(state, label, *, llm)`` — scan ALL ``input[type=file]`` on the page
    (INCLUDING hidden / zero-box ones; browser-use's serializer keeps file inputs in the
    selector_map even when not visible, serializer.py:704 ``is_file_input`` clause), and pick the
    one whose name / id / aria-label / nearby question text best matches the field's tokens
    (resume | cv | cover letter | portfolio) via the LLM picker — NO regex/substring.

  * ``is_already_uploaded(session, node)`` — BEFORE uploading, read whether a file is already
    attached: the input's ``.files`` is non-empty, OR a filename / "uploaded" / "remove"
    affordance is rendered in the control's group. Idempotent — "read if we've uploaded already"
    so a re-run never double-uploads.

REAL browser-use API reused (file:line, verified — NOT guessed; the read_options BrowserError
trap is exactly why these are studied):
  * selector_map node fields: ``backend_node_id``, ``node_name``/``tag_name``, ``attributes``,
    ``is_visible``, ``parent_node``/``children_nodes`` (live linkage, service.py:810/858/869),
    ``get_all_children_text`` (dom/views.py:561)  — same nodes oa_perception reads.
  * the live DOM read is the EXACT CDP shape oa_dom_value uses: ``cdp_client_for_node`` ->
    ``DOM.resolveNode`` -> ``Runtime.callFunctionOn(returnByValue=True)`` (oa_dom_value.py:136-153,
    itself mirroring default_action_watchdog.py:1145 / :2039). We reuse that plumbing, only the JS
    body differs (reads ``el.files.length`` + the group's rendered filename affordance).
"""

from __future__ import annotations

from typing import Any

import oa_perception as perc
import wd_repeaters as _wr


def _is_file_input(node: Any) -> bool:
    tag = (getattr(node, "node_name", "") or "").lower()
    attrs = getattr(node, "attributes", None) or {}
    return tag == "input" and (attrs.get("type") or "").lower() == "file"


def file_inputs(state: perc.OAState) -> list[Any]:
    """Every ``input[type=file]`` on the page — INCLUDING hidden / zero-box ones.

    browser-use keeps file inputs in the selector_map even when not visible (serializer.py:704),
    so this is the complete set the styled "Attach" buttons proxy for. We do NOT filter on
    visibility here — that is the whole point (a hidden file input is the normal case)."""
    return [n for n in state.selector_map.values() if _is_file_input(n)]


def _candidate_caption(node: Any) -> str:
    """A human-readable caption for ONE file input, for the LLM picker to reason over.

    Built from standard, non-renameable signals: the input's name/id/aria-label/accept attrs and
    the rendered text of its question group (``oa_perception._group_text`` — the same 'what the
    agent sees' text used by the spatial locate). No data-* / [for] hooks."""
    attrs = getattr(node, "attributes", None) or {}
    bits: list[str] = []
    for a in ("aria-label", "name", "id", "accept", "title"):
        v = attrs.get(a)
        if v and str(v).strip():
            bits.append(str(v).strip())
    group = ""
    try:
        group = perc._group_text(node)
    except Exception:
        group = ""
    if group:
        bits.append(group)
    cap = " | ".join(dict.fromkeys(b for b in bits if b))  # order-preserving de-dupe
    return cap or "file upload"


async def find_file_input(state: perc.OAState, label: str, *, llm: Any = None) -> Any | None:
    """Pick the ``input[type=file]`` that best matches ``label`` (resume/CV/cover letter/portfolio).

    GENERIC, no per-ATS code. Scans ALL file inputs (incl. hidden). If there is exactly one, it is
    the answer (the common single-resume case — no LLM needed). With several (resume + cover letter
    + portfolio), the LLM picker (``wd_repeaters._llm_pick`` — memoised, LLM-only, NO regex) chooses
    by the candidate captions vs the wanted field label. Returns the node or None."""
    inputs = file_inputs(state)
    if not inputs:
        return None
    if len(inputs) == 1:
        return inputs[0]

    captions = [_candidate_caption(n) for n in inputs]
    # Disambiguate the captions if any collide so the picker maps back unambiguously.
    seen: dict[str, int] = {}
    uniq: list[str] = []
    for c in captions:
        if c in seen:
            seen[c] += 1
            uniq.append(f"{c} #{seen[c]}")
        else:
            seen[c] = 1
            uniq.append(c)

    if llm is None:
        return inputs[0]
    chosen = await _wr._llm_pick(llm, label, uniq)
    if not chosen:
        return inputs[0]  # a file field is required-by-shape; default to the first real input
    try:
        return inputs[uniq.index(chosen)]
    except ValueError:
        return inputs[0]


# The already-uploaded probe. Bound to the located file input via ``this`` (callFunctionOn).
# Pure standard DOM: the input's own ``.files`` is the truth; a rendered filename / "remove" /
# "uploaded" affordance in the control's group is the secondary signal (some ATSes detach the
# input after a successful upload and render only the filename chip).
_UPLOADED_PROBE_JS = r"""
function() {
  try {
    const el = this;
    // 1) the input's own FileList — the authoritative "a file is attached" signal.
    if (el.files && el.files.length > 0) {
      const f = el.files[0];
      return (f && f.name) ? ("FILE:" + f.name) : "FILE:1";
    }
    // 2) a rendered filename / remove / uploaded affordance in the control's group.
    let root = el;
    for (let i = 0; i < 5 && root && root.parentElement; i++) {
      const r = root.parentElement;
      if (r && r.querySelectorAll && r.querySelectorAll("*").length > 1) { root = r; break; }
      root = r;
    }
    const txt = (root && root.textContent ? root.textContent : "").replace(/\s+/g, " ").trim();
    const low = txt.toLowerCase();
    if (/\.(pdf|docx?|rtf|txt|odt|pages)\b/.test(low) ||
        /\b(remove|delete|replace|uploaded|change file|re-upload)\b/.test(low)) {
      return "AFFORDANCE:" + txt.slice(0, 200);
    }
    return "";
  } catch (e) {
    return "";
  }
}
"""


async def is_already_uploaded(session: Any, node: Any) -> bool:
    """Read whether a file is ALREADY attached to / shown for this file input — idempotent guard.

    Uses the SAME CDP read plumbing as oa_dom_value (resolveNode -> callFunctionOn) so it is the
    proven path, only the JS differs. True when ``input.files`` is non-empty OR a filename / remove
    / uploaded affordance is rendered in the control's group. False on any failure (caller then
    uploads — a failed read must never silently skip a required upload)."""
    if node is None:
        return False
    backend_node_id = getattr(node, "backend_node_id", None)
    if backend_node_id is None:
        return False
    try:
        cdp_session = await session.cdp_client_for_node(node)
        session_id = cdp_session.session_id
        resolved = await cdp_session.cdp_client.send.DOM.resolveNode(
            params={"backendNodeId": int(backend_node_id)},
            session_id=session_id,
        )
        obj = (resolved or {}).get("object") or {}
        object_id = obj.get("objectId")
        if not object_id:
            return False
        result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
            params={
                "functionDeclaration": _UPLOADED_PROBE_JS,
                "objectId": object_id,
                "returnByValue": True,
            },
            session_id=session_id,
        )
    except Exception:
        return False
    val = ((result or {}).get("result") or {}).get("value")
    return bool(val and str(val).strip())


# --------------------------------------------------------------------------- #
# OFFLINE self-test — fake selector_map of file inputs + a fake CDP session whose
# uploaded-probe value is scripted. Proves: find_file_input picks the right input
# (single + multi via the LLM picker), hidden inputs are still found, and the
# already-uploaded read returns True/False on the scripted probe. $0, no browser.
# --------------------------------------------------------------------------- #
def _mk_file_input(bnid: int, *, name="", _id="", aria="", visible=True) -> Any:
    from browser_use.dom.views import DOMRect, EnhancedAXNode, EnhancedDOMTreeNode, NodeType

    attrs: dict[str, str] = {"type": "file"}
    if name:
        attrs["name"] = name
    if _id:
        attrs["id"] = _id
    ax = EnhancedAXNode(
        ax_node_id=f"ax{bnid}",
        ignored=False,
        role=None,
        name=aria or None,
        description=None,
        properties=None,
        child_ids=None,
    )
    return EnhancedDOMTreeNode(
        node_id=bnid,
        backend_node_id=bnid,
        node_type=NodeType.ELEMENT_NODE,
        node_name="INPUT",
        node_value="",
        attributes=attrs,
        is_scrollable=False,
        is_visible=visible,
        absolute_position=(
            DOMRect(x=0, y=0, width=0, height=0) if not visible else DOMRect(x=10, y=10, width=120, height=30)
        ),
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


async def _selftest() -> int:
    from oa_dom_value import _FakeCdpSend, _FakeCdpSession

    checks: list[tuple[str, bool, Any]] = []

    def chk(name: str, passed: bool, detail: Any = "") -> None:
        checks.append((name, passed, detail))

    # A reusable generic picker LLM (token overlap on captions).
    from oa_observe_act_fakes import GenericFakeLLM

    llm = GenericFakeLLM()

    # (1) single HIDDEN file input -> found without any LLM (the common resume case).
    hidden = _mk_file_input(1, name="resume", visible=False)
    st1 = perc.OAState(selector_map={1: hidden}, url="https://x/apply")
    got1 = await find_file_input(st1, "Resume/CV", llm=None)
    chk("single hidden file input found (no llm)", got1 is hidden, got1)
    chk("file_inputs includes hidden", file_inputs(st1) == [hidden])

    # (2) two file inputs (resume vs cover letter) -> LLM picker routes by caption.
    res = _mk_file_input(2, name="resume", aria="Resume")
    cov = _mk_file_input(3, name="cover_letter", aria="Cover Letter")
    st2 = perc.OAState(selector_map={2: res, 3: cov}, url="https://x/apply")
    got_res = await find_file_input(st2, "Resume", llm=llm)
    got_cov = await find_file_input(st2, "Cover Letter", llm=llm)
    chk("multi: resume field -> resume input", got_res is res, got_res)
    chk("multi: cover letter field -> cover input", got_cov is cov, got_cov)

    # (3) no file input at all -> None.
    st3 = perc.OAState(selector_map={}, url="https://x/apply")
    chk("no file input -> None", await find_file_input(st3, "Resume", llm=llm) is None)

    # (4) already-uploaded read: scripted probe value present -> True; empty -> False.
    class _Sess:
        def __init__(self, value: str) -> None:
            self._v = value

        async def cdp_client_for_node(self, node: Any) -> Any:
            return _FakeCdpSession(_FakeCdpSend(object_id="obj", value=self._v))

    chk("already-uploaded: files present -> True", await is_already_uploaded(_Sess("FILE:resume.pdf"), res) is True)
    chk(
        "already-uploaded: affordance -> True",
        await is_already_uploaded(_Sess("AFFORDANCE: resume.pdf  Remove"), res) is True,
    )
    chk("already-uploaded: empty -> False", await is_already_uploaded(_Sess(""), res) is False)
    chk("already-uploaded: None node -> False", await is_already_uploaded(_Sess("x"), None) is False)

    ok = True
    print("\n=== oa_file_locate offline self-test (fake selector_map + CDP, no browser, $0) ===")
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_selftest()))
