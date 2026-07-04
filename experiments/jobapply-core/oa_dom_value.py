"""oa_dom_value — read the CURRENT live value of a located control GENERICALLY, free, no VLM.

This is the verify ORACLE's PRIMARY truth source (OBSERVE_ACT_DESIGN.md §6 / the verify-oracle
fix). A text/email/phone/url/date value is already IN THE DOM — the control's `.value`, the
selected <option> text, or the committed combobox display / chip text. Reading it back is free
and instant, so the VLM (capped + slow) is demoted to an AID for visual-only widgets only.

REAL browser-use API used (verified against the vendored tree — NOT guessed; the read_options
BrowserError trap is exactly why this is studied rather than invented):

  * ``BrowserSession.cdp_client_for_node(node) -> CDPSession``
        browser/session.py:3788  (resolves the right frame's CDP session for a node)
  * ``cdp_session.cdp_client.send.DOM.resolveNode(params={'backendNodeId': <id>}, session_id=…)``
        -> ``result['object']['objectId']``
        (watchdog default_action_watchdog.py:1145-1152 — the exact resolve pattern)
  * ``cdp_session.cdp_client.send.Runtime.callFunctionOn(params={'functionDeclaration': <fn>,
        'objectId': object_id, 'returnByValue': True}, session_id=…)``  -> ``result['result']['value']``
        (watchdog :1657 _clear_text_field and :2039 _set_value_directly — the EXACT call shape:
         a ``function() { … }`` body bound to ``this``, returnByValue=True, read off result.result.value)

The JS reads the live displayed value the way a human reads the filled control, with NO
renameable-attribute dependency (no data-automation-id / [for]); it walks STANDARD DOM:
  - native form controls: input/textarea ``.value``; <select> selected-option text.
  - contenteditable: ``.textContent``.
  - combobox / react-select: the input's own ``.value`` (typeahead text), else the committed
    SINGLE-VALUE display text or the committed CHIP/PILL texts found inside the control's
    container (role=combobox / the closest wrapper) — read from rendered text, joined.

Returns "" when the control has no readable DOM value (a visual-only widget) so the caller
escalates to the VLM aid rather than concluding EMPTY.
"""

from __future__ import annotations

from typing import Any

# The generic live-value reader. Bound to the located control via ``this`` (callFunctionOn).
# Pure standard-DOM; returns a STRING (possibly "") — never throws across the CDP boundary.
_READ_VALUE_JS = r"""
function() {
  try {
    const el = this;
    const tag = (el.tagName || "").toUpperCase();
    const norm = (s) => (s == null ? "" : String(s)).replace(/\s+/g, " ").trim();

    // 1) native <select> — the selected option's visible text (selectedOptions is the truth).
    if (tag === "SELECT") {
      const sel = el.selectedOptions && el.selectedOptions.length
        ? Array.from(el.selectedOptions)
        : (el.options ? Array.from(el.options).filter(o => o.selected) : []);
      const txt = sel.map(o => norm(o.label || o.textContent || o.value)).filter(Boolean);
      // a placeholder first option ("Select…") with value "" is NOT a filled value.
      const real = txt.filter((t, i) => !(sel[i] && (sel[i].value === "" )));
      return real.join(", ");
    }

    // 2a) checkbox / radio — .value is the SUBMIT payload and exists even when UNCHECKED; the
    // truth is the checked state. Unchecked -> "" (reads EMPTY, so the engine commits). This
    // killed a live lie: a consent checkbox's value string LLM-matched the wanted answer and the
    // pre-check skipped the field while the box sat unchecked (breezy gdpr).
    const ty2 = (el.type || "").toLowerCase();
    if (tag === "INPUT" && (ty2 === "checkbox" || ty2 === "radio")) {
      if (!el.checked) return "";
      const lab = el.labels && el.labels[0] ? norm(el.labels[0].textContent) : "";
      return norm(el.value && el.value !== "on" ? el.value : "") || lab || "checked";
    }

    // 2) input / textarea — the live .value (typeahead text counts: the user typed it).
    if (tag === "INPUT" || tag === "TEXTAREA") {
      const v = norm(el.value);
      if (v) return v;
      // a react-select INPUT is usually empty after commit; the committed value renders as a
      // sibling single-value / chip — fall through to the container scan below.
    }

    // 3) contenteditable — the rendered text content.
    const ce = el.getAttribute && (el.getAttribute("contenteditable"));
    if (ce === "" || ce === "true" || el.isContentEditable === true) {
      const v = norm(el.textContent);
      if (v) return v;
    }

    // 4) combobox / react-select committed display — scan the control's container for the
    //    rendered single-value text or committed chips/pills. Generic: we look at the closest
    //    role=combobox / wrapper and read its visible value-bearing descendants, EXCLUDING the
    //    text input itself (already handled) and any placeholder.
    let root = el;
    // climb to a small wrapper (react-select renders value as a sibling of the input)
    for (let i = 0; i < 4 && root && root.parentElement; i++) {
      const r = root.parentElement;
      // stop climbing once we have a container that holds more than just the input
      if (r && r.querySelectorAll && r.querySelectorAll("*").length > 1) { root = r; break; }
      root = r;
    }
    if (root && root.querySelectorAll) {
      // common committed-value carriers across react-select / generic comboboxes / chip lists.
      const sels = [
        '[class*="singleValue"]', '[class*="single-value"]',
        '[class*="multiValue"] [class*="label"]', '[class*="multi-value"] [class*="label"]',
        '[class*="multiValue"]', '[class*="multi-value"]',
        '[class*="chip"]', '[class*="tag"]', '[class*="pill"]',
        '[role="option"][aria-selected="true"]',
        '[aria-selected="true"]'
      ];
      const seen = new Set();
      const out = [];
      for (const s of sels) {
        let nodes = [];
        try { nodes = Array.from(root.querySelectorAll(s)); } catch (e) { nodes = []; }
        for (const n of nodes) {
          // skip the placeholder and the input itself
          const cls = (n.className && n.className.baseVal != null) ? n.className.baseVal : (n.className || "");
          if (typeof cls === "string" && /placeholder/i.test(cls)) continue;
          if (n === el) continue;
          // never read LABEL text as a committed value: the wrapper scan grabbed the QUESTION
          // ('What is your preferred office location?') and verify blessed junk as CORRECT
          // (robinhood). Structural exclusion — the node is/inside a <label> or IS the
          // control's own label.
          if (n.tagName === 'LABEL' || (n.closest && n.closest('label'))) continue;
          if (el.labels && Array.from(el.labels).some(L => L === n || L.contains(n))) continue;
          const t = norm(n.textContent);
          if (t && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); out.push(t); }
        }
        if (out.length) break;  // first selector family that yields anything wins (most specific)
      }
      if (out.length) return out.join(", ");
    }

    return "";
  } catch (e) {
    return "";
  }
}
"""


async def read_dom_value(session: Any, node: Any) -> str:
    """Read the located control's CURRENT live displayed value, GENERICALLY, via CDP — free, no VLM.

    Resolves the node to a Runtime objectId (DOM.resolveNode) and runs ``_READ_VALUE_JS`` bound to
    it (Runtime.callFunctionOn, returnByValue=True), returning the rendered value string. Covers
    input/textarea ``.value``, native <select> selected text, contenteditable text, and committed
    combobox/react-select single-value or chip text.

    Returns "" on any failure or an unreadable/visual-only widget (caller -> VLM aid, NOT EMPTY).
    """
    if node is None:
        return ""
    backend_node_id = getattr(node, "backend_node_id", None)
    if backend_node_id is None:
        return ""
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
            return ""
        result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
            params={
                "functionDeclaration": _READ_VALUE_JS,
                "objectId": object_id,
                "returnByValue": True,
            },
            session_id=session_id,
        )
    except Exception:
        return ""
    val = ((result or {}).get("result") or {}).get("value")
    if val is None:
        return ""
    return str(val).strip()


# --------------------------------------------------------------------------- #
# OFFLINE self-test — a FAKE session whose CDP layer returns scripted values for
# resolveNode + callFunctionOn, so read_dom_value's plumbing is proven at $0,
# no browser. (The JS body itself is exercised live; here we prove the call shape
# + the empty/None/error degradation contract.)
# --------------------------------------------------------------------------- #
class _FakeCdpSend:
    def __init__(self, *, object_id: str | None, value: Any) -> None:
        self._object_id = object_id
        self._value = value

        async def _resolve(params: Any = None, session_id: Any = None) -> dict:
            if self._object_id is None:
                return {"object": {}}
            return {"object": {"objectId": self._object_id}}

        async def _call(params: Any = None, session_id: Any = None) -> dict:
            return {"result": {"value": self._value}}

        self.DOM = type("DOM", (), {"resolveNode": staticmethod(_resolve)})()
        self.Runtime = type("Runtime", (), {"callFunctionOn": staticmethod(_call)})()


class _FakeCdpClient:
    def __init__(self, send: _FakeCdpSend) -> None:
        self.send = send


class _FakeCdpSession:
    def __init__(self, send: _FakeCdpSend) -> None:
        self.session_id = "sess-1"
        self.cdp_client = _FakeCdpClient(send)


class _FakeNode:
    def __init__(self, backend_node_id: int | None) -> None:
        self.backend_node_id = backend_node_id


class _FakeValueSession:
    """Minimal stand-in: ``cdp_client_for_node`` returns a CDP session whose resolveNode +
    callFunctionOn are scripted (object_id present/absent, value present/None/raise)."""

    def __init__(self, *, object_id: str | None = "obj-1", value: Any = "", raise_on_call: bool = False) -> None:
        self._object_id = object_id
        self._value = value
        self._raise = raise_on_call

    async def cdp_client_for_node(self, node: Any) -> _FakeCdpSession:
        if self._raise:
            raise RuntimeError("cdp boom")
        return _FakeCdpSession(_FakeCdpSend(object_id=self._object_id, value=self._value))


async def _selftest() -> int:
    checks: list[tuple[str, bool, Any]] = []

    def chk(name: str, passed: bool, detail: Any = "") -> None:
        checks.append((name, passed, detail))

    node = _FakeNode(42)

    v = await read_dom_value(_FakeValueSession(value="Pyry"), node)
    chk("reads scripted value", v == "Pyry", v)

    v_empty = await read_dom_value(_FakeValueSession(value=""), node)
    chk("empty value -> ''", v_empty == "", v_empty)

    v_none = await read_dom_value(_FakeValueSession(value=None), node)
    chk("None value -> ''", v_none == "", v_none)

    v_noobj = await read_dom_value(_FakeValueSession(object_id=None, value="x"), node)
    chk("no objectId -> '' (degrade, never crash)", v_noobj == "", v_noobj)

    v_raise = await read_dom_value(_FakeValueSession(raise_on_call=True, value="x"), node)
    chk("cdp raises -> '' (swallowed)", v_raise == "", v_raise)

    v_nonode = await read_dom_value(_FakeValueSession(value="x"), None)
    chk("None node -> ''", v_nonode == "", v_nonode)

    v_nobnid = await read_dom_value(_FakeValueSession(value="x"), _FakeNode(None))
    chk("node without backend id -> ''", v_nobnid == "", v_nobnid)

    v_strip = await read_dom_value(_FakeValueSession(value="  spaced  "), node)
    chk("value stripped", v_strip == "spaced", v_strip)

    ok = True
    print("\n=== oa_dom_value offline self-test (fake CDP, no browser, $0) ===")
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_selftest()))
