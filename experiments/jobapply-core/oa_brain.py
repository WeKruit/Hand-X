"""oa_brain — the thin LLM/VLM brain for the generic ``observe_act`` fill primitive.

This is the genuine NET-NEW intelligence the design (OBSERVE_ACT_DESIGN.md §4/§5/§6)
calls out: browser-use already owns PERCEPTION (DomService delta/coords/visibility) and
ACTION (tools: read/select dropdown, trusted click/type/scroll). It has NO field-nature
classifier, NO typeahead search-loop, and NO deterministic value-aware verify. ``oa_brain``
supplies exactly those three, and NOTHING else — every heavier capability is reused:

  * the cheap structured TEXT pick  -> ``wd_repeaters._llm_pick`` (memoised; $0.0002/call)
  * the value-aware VLM verify       -> ``vision_verify.visual_check`` + ``_matches``/``_is_filled``
                                        (~$0.0006/call, per-(url|label|want) cached, 6/page capped)

The four public functions:

  classify_nature(label_text, value, hints)  -> Nature        (ONE cheap LLM call; §4 Gap-B)
  query_variants(value, nature)              -> list[str]     (ordered typeahead queries; §5)
  pick_option(value, option_texts, *, llm)   -> str | None    (reuse _llm_pick; §5 MATCH)
  verify(session, label_text, value, *, ...) -> Verdict       (value-aware VLM 3-way; §6)

DESIGN INVARIANTS honoured here:
  - Gap B (§4.4): "no delta" is NEVER assumed free-text. ``classify_nature`` requires a
    POSITIVE free-text signal; when the model is unsure it returns ``UNKNOWN`` so the caller
    escalates rather than rubber-stamping a blind type into a closed widget.
  - §6.1 routing: ``verify`` returns CORRECT | EMPTY | WRONG | UNKNOWN — EMPTY routes to
    re-commit (the click never registered), WRONG to re-search (committed the wrong option),
    UNKNOWN to the caller's escalate/skip policy. It NEVER collapses EMPTY and WRONG.
  - Determinism over the model: deterministic post-conditions (options present -> closed_list,
    yes/no value -> boolean) are applied in CODE here, not trusted to the LLM (§4.3).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Literal

import oa_llm as _oa_llm
import vision_verify as _vv
import wd_repeaters as _wr

# --------------------------------------------------------------------------- #
# Field nature — the routing taxonomy the state machine (§2) keys on.
# CLOSED_LIST : short fixed menu rendered in full on one click (Degree, Gender, State)
# SEARCH      : look up a value by TYPING into a combobox over a large vocab (School, City, Skill)
# FREE_TEXT   : prose / arbitrary string, no controlled vocabulary (Why…, URL, preferred name)
# DATE        : a date
# BOOLEAN     : yes/no / agree
# MULTI       : N values into one widget (Skills, Languages) -> S_MULTI_LOOP
# UNKNOWN     : the model could not commit; caller ESCALATES (never types blindly — Gap B)
# --------------------------------------------------------------------------- #
Nature = Literal["CLOSED_LIST", "SEARCH", "FREE_TEXT", "DATE", "BOOLEAN", "MULTI", "UNKNOWN"]
_NATURES: frozenset[str] = frozenset({"CLOSED_LIST", "SEARCH", "FREE_TEXT", "DATE", "BOOLEAN", "MULTI", "UNKNOWN"})

# The model speaks the design's §4.2 vocabulary; we map it onto Nature. ``many`` is a
# cardinality the rubric emits alongside a base nature, so it is handled separately.
_MODEL_TO_NATURE: dict[str, Nature] = {
    "closed_list": "CLOSED_LIST",
    "searchable": "SEARCH",
    "free_text": "FREE_TEXT",
    "date": "DATE",
    "boolean": "BOOLEAN",
}

# 3-way (plus UNKNOWN) verify verdict — §6.1.
Verdict = Literal["CORRECT", "EMPTY", "WRONG", "UNKNOWN"]

# Tokens that, as the WHOLE wanted value, deterministically mean a boolean field (§4.3 post-cond).
_BOOL_VALUES: frozenset[str] = frozenset(
    {
        "yes",
        "no",
        "true",
        "false",
        "i agree",
        "agree",
        "i consent",
        "consent",
        "i accept",
        "accept",
        "decline",
        "y",
        "n",
    }
)


@dataclass
class ClassifyHints:
    """OPTIONAL deterministic signals the caller already has (all default-off so a bare
    ``classify_nature(label, value)`` works). These let CODE override the model per §4.3 —
    they are facts (the DOM read options / a known multi-label), not guesses.

    options        : non-empty -> nature coerced to CLOSED_LIST (or BOOLEAN if options ⊆ yes/no)
    known_multi    : a known multi-value label (Skills/Languages/Technologies) -> MULTI
    value_is_list  : the value is a comma-joined set the caller wants spread across pills.

    MULTI mis-route fix: ``value_is_list`` ALONE is NOT a multi signal — a single location/city
    value ("San Francisco, CA") naturally carries a comma, and the old "comma -> MULTI" rule sent
    it to S_MULTI_LOOP and tried to type "San Francisco" then "CA" as separate pills. A comma only
    means MULTI when the FIELD genuinely takes many values, i.e. ``known_multi`` is also set (a
    Skills/Languages/Technologies label). So MULTI now requires ``known_multi`` (a real multi-value
    field); a comma in the value of a single-value field is ignored here and the field classifies on
    its label meaning (a location -> SEARCH).
    """

    options: list[str] = field(default_factory=list)
    known_multi: bool = False
    value_is_list: bool = False


# Label tokens that genuinely mark a MULTI-value field (N values into one widget -> pills). The ONLY
# things that license the comma-in-value -> MULTI route; a location/city comma must never reach it.
_MULTI_LABEL_TOKENS: frozenset[str] = frozenset(
    {"skill", "skills", "language", "languages", "technology", "technologies", "tool", "tools"}
)


def is_multi_label(label_text: str) -> bool:
    """True iff the field's VISIBLE label names a genuine multi-value field (Skills/Languages/
    Technologies/Tools). Used to gate the comma-in-value -> MULTI route so a single location/city
    value with a comma ("San Francisco, CA") is NEVER spread across pills. Substring match on the
    label's lower-cased tokens — no per-ATS string, purely the human-readable label meaning."""
    low = (label_text or "").lower()
    return any(tok in low for tok in _MULTI_LABEL_TOKENS)


# --------------------------------------------------------------------------- #
# 1. classify_nature — ONE cheap LLM call over the VISIBLE label + value (§4, Gap B).
# --------------------------------------------------------------------------- #
async def classify_nature(
    label_text: str,
    value: str,
    hints: ClassifyHints | None = None,
    *,
    llm: Any = None,
) -> Nature:
    """Decide the field's nature from its VISIBLE label meaning + the wanted value.

    Deterministic post-conditions (§4.3) are applied in CODE *before and after* the model so
    a model wobble cannot defeat a known fact:
      * ``hints.options`` non-empty  -> CLOSED_LIST (BOOLEAN if those options are just yes/no)
      * ``hints.known_multi``         -> MULTI (a comma in the value alone does NOT — see ClassifyHints)
      * a bare yes/no/agree value     -> BOOLEAN

    Gap-B safety: the model's ``free_text`` answer is the ONLY path to FREE_TEXT, and it is a
    POSITIVE signal — there is no "no signal -> free_text" fallback here. If ``llm is None`` or
    the call fails, we return ``UNKNOWN`` (the caller escalates / runs the DOM-positive text test
    at S_TEXT_GUARD), NEVER a silent FREE_TEXT that would license a blind type.
    """
    h = hints or ClassifyHints()

    # --- deterministic PRE-conditions (facts win over the model) ---------------
    if h.options:
        opt_keys = {_norm_lower(o) for o in h.options if o and o.strip()}
        if opt_keys and opt_keys <= _BOOL_VALUES:
            return "BOOLEAN"
        if opt_keys:
            return "CLOSED_LIST"
    # MULTI requires a GENUINE multi-value field (known_multi). A comma in the value (value_is_list)
    # is only honoured WHEN the field is also a known multi-value field — otherwise a single location
    # value "San Francisco, CA" would be split across pills (the location -> MULTI mis-route). A bare
    # comma on a single-value field is ignored here; the field classifies on its label meaning below.
    if h.known_multi:
        return "MULTI"
    if _norm_lower(value) in _BOOL_VALUES:
        return "BOOLEAN"

    # --- the ONE cheap model call (§4.2 rubric) --------------------------------
    raw = await _classify_llm(label_text, value, llm=llm)
    if raw is None:
        return "UNKNOWN"  # unsure -> caller escalates; we DO NOT guess free_text (Gap B)
    base, cardinality = raw

    # --- deterministic POST-conditions -----------------------------------------
    if cardinality == "many":
        return "MULTI"
    return _MODEL_TO_NATURE.get(base, "UNKNOWN")


async def _classify_llm(label_text: str, value: str, *, llm: Any) -> tuple[str, str] | None:
    """The single structured classify call. Returns ``(base_nature, cardinality)`` from the
    §4.2 vocabulary, or ``None`` on no-llm / error / an out-of-vocab reply (caller -> UNKNOWN)."""
    if llm is None:
        return None
    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class _Nat(BaseModel):
        nature: str  # closed_list | searchable | free_text | date | boolean
        cardinality: str = "one"  # one | many

    system = (
        "Classify ONE web-form field by what KIND of input it is, judging ONLY from its visible "
        "label meaning and the value to enter — never from machine names. Reply 'nature' as ONE of:\n"
        "  closed_list - pick one from a SHORT fixed menu shown in full on a single click "
        "(Degree, Gender, Employment type, State, 'How did you hear about us').\n"
        "  searchable  - look up a value by TYPING into a combobox over a LARGE vocabulary "
        "(School, University, Field of Study, Country, City, Location, Skill, Employer, Language). "
        "When unsure between searchable and closed_list, PREFER searchable.\n"
        "  free_text   - prose or an arbitrary string with NO controlled vocabulary (a '?' question, "
        "Why/Describe/Tell us/cover letter; URL/LinkedIn/GitHub/preferred name/salary/address line).\n"
        "  date        - any date.\n"
        "  boolean     - a yes/no / agree / consent toggle.\n"
        "Set 'cardinality' to 'many' ONLY if the field clearly takes MULTIPLE values "
        "(Skills, Languages, Technologies); else 'one'. Reply strictly."
    )
    with contextlib.suppress(Exception):
        # BOUNDED + fallback-capable: a stalled gemini fails fast (OA_LLM_TIMEOUT) and a second
        # provider answers, instead of an unbounded ainvoke hanging the field ~34s. None -> UNKNOWN.
        res = await _oa_llm.resilient_text(
            [SystemMessage(content=system), UserMessage(content=f"label: {label_text!r}\nvalue: {value!r}")],
            output_format=_Nat,
            primary=llm,
        )
        if res is None:
            return None
        base = (res.completion.nature or "").strip().lower()
        card = (res.completion.cardinality or "one").strip().lower()
        if base not in _MODEL_TO_NATURE:
            return None  # out-of-vocab -> UNKNOWN, never a silent FREE_TEXT
        return base, ("many" if card == "many" else "one")
    return None


# --------------------------------------------------------------------------- #
# 2. query_variants — ordered typeahead queries (§5: most-canonical first, capped at 3).
# --------------------------------------------------------------------------- #
_VARIANT_CAP = 3  # §6.2 VARIANT_CAP — shared front + revalue, deduped.
_PREFIX_LEN = 12  # a "long" value gets a short distinctive prefix tried first (server filters on it).


async def query_variants(value: str, nature: Nature, *, llm: Any = None) -> list[str]:
    """Ordered, deduped query strings to try in a typeahead, most-canonical first, ``<= 3``.

    For non-search natures (or a blank value) the only sensible query is the value itself.
    For SEARCH/MULTI we ask the cheap LLM for canonical variants (e.g. 'UCLA' /
    'University of California, Los Angeles'); a long value also yields a short PREFIX so the
    server's result cap is hit on a distinctive stem rather than the whole string (§5
    'long values -> a short prefix first'). On no-llm / error we degrade to a deterministic
    [value, prefix] — never empty (the caller always has at least the raw value to type)."""
    v = (value or "").strip()
    if not v:
        return []
    if nature not in ("SEARCH", "MULTI"):
        return [v]

    variants: list[str] = [v]
    llm_variants = await _variant_llm(v, llm=llm)
    variants.extend(llm_variants)
    # Deterministic long-value prefix as a guaranteed fallback query (§5 server-cap guard front-end).
    if len(v) > _PREFIX_LEN:
        variants.append(v[:_PREFIX_LEN].strip())

    return _dedupe_cap(variants, _VARIANT_CAP)


async def _variant_llm(value: str, *, llm: Any) -> list[str]:
    """Cheap call for canonical typeahead variants of ``value`` (acronym <-> full name, etc.).
    Returns [] on no-llm / error so ``query_variants`` falls back deterministically."""
    if llm is None:
        return []
    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class _Vars(BaseModel):
        variants: list[str]  # most-canonical first, e.g. ["University of California, Los Angeles", "UCLA"]

    system = (
        "Give up to 3 SEARCH query variants for the given value, to type into a job-application "
        "typeahead/autocomplete. Order MOST-CANONICAL first (the full official name), then common "
        "acronyms / shortenings (e.g. 'UCLA' -> ['University of California, Los Angeles', 'UCLA', "
        "'California Los Angeles']). Keep each a plausible thing a real autocomplete would index. "
        "Do not invent unrelated entities. Reply strictly."
    )
    with contextlib.suppress(Exception):
        # BOUNDED + fallback-capable (see _classify_llm). None -> [] (caller degrades to [value, prefix]).
        res = await _oa_llm.resilient_text(
            [SystemMessage(content=system), UserMessage(content=f"value: {value!r}")],
            output_format=_Vars,
            primary=llm,
        )
        if res is None:
            return []
        return [s.strip() for s in (res.completion.variants or []) if s and s.strip()]
    return []


# --------------------------------------------------------------------------- #
# 3. pick_option — reuse the memoised cheap text picker (§5 MATCH).
# --------------------------------------------------------------------------- #
async def pick_option(value: str, option_texts: list[str], *, llm: Any = None, label: str = "") -> str | None:
    """Best-matching option text for ``value`` from the READ option list, or ``None`` if none fit.

    Thin pass-through to ``wd_repeaters._llm_pick`` — the proven, memoised-on-(value,options,label)
    cheap structured picker (LLM-only, no substring/regex, no vision). ``label`` is the field's
    QUESTION: without it identical option sets under different questions pick blindly (audit #1).
    Returns the EXACT option string (so the caller can cross-check ``committed_text`` at verify, §6.1)."""
    if not option_texts:
        return None
    return await _wr._llm_pick(llm, value, option_texts, label)


# --------------------------------------------------------------------------- #
# 4. verify — DOM read-back FIRST (free), VLM as a budgeted AID (§6.1).
#    The verify ORACLE: a text/email/phone/url/date value is ALREADY in the DOM
#    (the control's .value / selected option / committed chip) — read it back for
#    free and LLM-match it; only consult the (capped, slow) VLM when the DOM read is
#    empty/ambiguous OR the widget is visual-only. Per-FIELD VLM budget, not per-page.
# --------------------------------------------------------------------------- #
async def verify(
    session: Any,
    label_text: str,
    value: str,
    *,
    node: Any = None,
    llm: Any = None,
    key: str | None = None,
    use_cache: bool = True,
    allow_vlm: bool = True,
) -> Verdict:
    """Value-aware read-back of one field -> the §6.1 3-way (+UNKNOWN) verdict.

    DOM-FIRST oracle (the whole point — free, instant, no VLM):
      1. Read the located control's CURRENT live value via ``oa_dom_value.read_dom_value`` (input/
         textarea ``.value``, native <select> selected text, contenteditable text, committed
         combobox/react-select single-value or chip text — all generic standard-DOM, no renameable
         attribute). Normalize + LLM-match it against ``value`` (reuse ``pick_option`` -> ``_llm_pick``;
         LLM-only, NO substring/regex):
            DOM value present & matches ``value``     -> CORRECT  (ZERO VLM calls)
            DOM value present & does NOT match        -> WRONG
            DOM value empty/unreadable                -> step 2 (do NOT conclude EMPTY for a visual widget)

      2. VLM AID (only on an empty/ambiguous DOM read, and only if ``allow_vlm`` and the per-field
         VLM budget allows): ``vision_verify.visual_check(want=value)`` parsed by ``route_verdict``.
         When the VLM is not consulted (budget spent / disallowed) we return EMPTY if the DOM was
         readably blank for a plain text control, else UNKNOWN.

    ``node`` is the located EnhancedDOMTreeNode (enables the free DOM read; without it we go
    straight to the VLM aid as the legacy path did). ``llm`` powers the DOM-value match. ``key``
    overrides the VLM cache identity so a re-commit's re-read uses a FRESH key (§6.2)."""
    # --- step 1: free DOM read-back ------------------------------------------
    dom_val = ""
    if node is not None:
        from oa_dom_value import read_dom_value

        with contextlib.suppress(Exception):
            dom_val = await read_dom_value(session, node)

    if dom_val:
        matched = await _dom_value_matches(value, dom_val, llm=llm, label=label_text)
        if matched:
            return "CORRECT"  # free truth — no VLM spent (the whole point)
        return "WRONG"  # a definite, different non-blank value is in the control

    # --- step 2: VLM AID (empty/ambiguous DOM read OR visual-only widget) -----
    if not allow_vlm:
        # caller's per-field VLM budget spent / disallowed -> don't spend.
        return "EMPTY" if node is not None else "UNKNOWN"
    verdict = await _vv.visual_check(session, label_text, want=value, key=key, use_cache=use_cache)
    return route_verdict(verdict)


async def _dom_value_matches(value: str, dom_value: str, *, llm: Any, label: str = "") -> bool:
    """Does the read-back DOM ``dom_value`` mean the same as the wanted ``value``? LLM-only match
    (reuse the memoised ``_llm_pick`` over a 2-option set), with a deterministic exact-normal-equality
    short-circuit (NOT substring/regex — full normalized-string identity, which IS deterministic and
    cheap). ``label`` = the field's own question — used to REJECT a read-back that is actually the
    field's LABEL/placeholder text leaked by the DOM reader (audit incident: verify blessed a label
    as a committed value)."""
    nv, nd = _norm_lower(value), _norm_lower(dom_value)
    # LABEL-LEAK GUARD (deterministic): the DOM reader sometimes returns the field's own question
    # text as its 'value' (a container scan grabbing the label). That is never a real answer — a
    # value equal to (or a leading chunk of) the label is WRONG, even if the LLM would call it a
    # match. Only fires when the value we WANTED is not itself that text.
    nl = _norm_lower(label)
    if nl and nd and nv != nl and (nd == nl or (len(nl) > 12 and nl.startswith(nd)) or (len(nd) > 12 and nd.startswith(nl))):
        return False
    if nv and nv == nd:
        return True  # exact normalized identity — deterministic, no LLM
    if llm is None:
        return False  # no matcher -> cannot affirm equivalence -> treat as WRONG (route to revalue)
    # LLM picker: which of {dom_value, "<no match>"} best matches the wanted value?
    sentinel = "— none of these —"
    picked = await _wr._llm_pick(llm, value, [dom_value, sentinel], label)
    return picked is not None and _norm_lower(picked) == nd


# --------------------------------------------------------------------------- #
# 5. pick_control_by_vision — locate Tier-3 AID: VLM disambiguates a SPATIAL TIE only.
#    Used ONLY when >=2 candidate controls tie spatially for one question (oa_perception's
#    structure + spatial tiers could not separate them). Bounded, AID-only, never primary.
# --------------------------------------------------------------------------- #
async def pick_control_by_vision(
    session: Any, label_text: str, candidates: list[Any], *, llm: Any = None
) -> Any | None:
    """Pick which candidate control belongs to the question ``label_text`` by ONE cheap VLM look.

    Each candidate is captioned by its question-group text + on-page box position (reusing
    ``oa_perception._group_text`` / ``node_rect`` — the same 'what the agent sees' signals the
    spatial tier uses). The VLM is shown the page screenshot and asked WHICH numbered candidate is
    the input for this question; we map its choice back to the node. Spends exactly ONE call through
    ``vision_verify``'s screenshot+counter so the caller's per-field VLM budget accounts for it.
    Returns the chosen node, or None on any failure / no clear pick (caller falls back)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    import oa_perception as _perc

    captions: list[str] = []
    for i, n in enumerate(candidates):
        gt = ""
        with contextlib.suppress(Exception):
            gt = _perc._group_text(n)
        rect = None
        with contextlib.suppress(Exception):
            rect = _perc.node_rect(n)
        pos = f"box≈(x={int(rect[0])},y={int(rect[1])})" if rect else "box≈unknown"
        captions.append(f"[{i}] {gt[:80] or '(no text)'} — {pos}")

    # Budget guard: a capped page returns the sentinel without a screenshot (no spend).
    if _vv._VLM_CALLS["n"] >= _vv.VLM_MAX_CALLS:
        return None
    png = await _oa_llm.bounded_screenshot(session)  # bounded — a stalled screenshot can't hang the field
    if png is None:
        return None
    import base64

    b64 = base64.b64encode(png).decode()
    prompt = (
        f'This is a job-application web form. The question is "{label_text}". Below are candidate '
        f"input controls, each numbered, with the visible text near it and its on-page position:\n"
        + "\n".join(captions)
        + '\nReply STRICT JSON {"index": <the number of the control that is the ANSWER box for this '
        "question>} — the input sitting directly under / beside that question. If none clearly fits, "
        'reply {"index": -1}.'
    )
    try:
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg: Any = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
    except Exception:
        msg = prompt
    # BOUNDED + vision-fallback-capable: a stalled gemini-vision fails fast and (if keyed) a vision
    # fallback answers; None -> caller falls back (no unbounded ainvoke).
    resp = await _oa_llm.resilient_vlm([msg], primary=_vv._vlm())
    if resp is None:
        return None
    raw = (getattr(resp, "completion", None) or str(resp)).strip()
    _vv._VLM_CALLS["n"] += 1  # count the spend so the per-field budget sees it (same as visual_check)

    idx = _parse_index(raw)
    if idx is None or idx < 0 or idx >= len(candidates):
        return None
    return candidates[idx]


# --------------------------------------------------------------------------- #
# 6. pick_control_by_marks — VISUAL SET-OF-MARKS bind for a LABEL-FREE card.
#    The locate LAST RESORT when STRUCTURE + spatial + grouped-text all miss (a non-text
#    card whose heading shares NO tokens with any control — a rich/imaged heading, an icon
#    radio group). We mark the candidate controls on the screenshot the way browser-use's own
#    set-of-marks does (numbered boxes) and ask the cheap VLM which marked control is the answer
#    for THIS question — binding a card the way a HUMAN SEES it, with no renameable label.
#
#    REAL set-of-marks API (verified, NOT guessed):
#      browser_use/browser/python_highlights.py:409 create_highlighted_screenshot(
#          screenshot_b64, selector_map, ..., filter_highlight_ids=False) — draws a numbered
#      dashed box per element; with filter_highlight_ids=False the overlay number is EXACTLY
#      ``str(element.backend_node_id)`` (process_element_highlight :396). So a selector_map of
#      ONLY the candidate controls yields a screenshot where each candidate is boxed + labeled
#      with its backend_node_id, and the VLM's chosen number maps straight back to the node.
# --------------------------------------------------------------------------- #
async def pick_control_by_marks(session: Any, label_text: str, candidates: list[Any], *, llm: Any = None) -> Any | None:
    """Bind a label-free card by VISUAL set-of-marks: mark the candidate controls on the page
    screenshot (browser-use ``create_highlighted_screenshot``, numbered by backend_node_id) and ask
    the cheap VLM which marked control is the input for the question ``label_text``. ONE screenshot +
    ONE cheap VLM call, counted through ``vision_verify``'s per-page/-field budget. Returns the chosen
    node, or None on any failure / no clear pick (caller falls back). GENERIC — no label/aria/data-*."""
    cands = [c for c in candidates if getattr(c, "backend_node_id", None) is not None]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # Budget guard: a capped page returns no pick without a screenshot (no spend).
    if _vv._VLM_CALLS["n"] >= _vv.VLM_MAX_CALLS:
        return None
    png = await _oa_llm.bounded_screenshot(session)  # bounded — a stalled screenshot can't hang the field
    if png is None:
        return None
    import base64

    raw_b64 = base64.b64encode(png).decode()

    # Build the set-of-marks: a selector_map of ONLY the candidate controls. Each draws a numbered
    # box labeled with its backend_node_id (filter_highlight_ids=False), so the VLM's number == node.
    by_id = {int(c.backend_node_id): c for c in cands}
    try:
        from browser_use.browser.python_highlights import create_highlighted_screenshot

        marked_b64 = await create_highlighted_screenshot(raw_b64, by_id, filter_highlight_ids=False)
    except Exception:
        return None

    legend = ", ".join(str(i) for i in by_id)
    prompt = (
        f"This is a job-application web form screenshot with candidate input controls outlined by "
        f'dashed boxes, each labeled with a NUMBER ({legend}). The question is "{label_text}". '
        f'Reply STRICT JSON {{"mark": <the NUMBER of the boxed control that is the ANSWER for this '
        f"question — the input/radio/checkbox/select/textarea sitting directly under or beside that "
        f'question heading>}}. If none of the boxed controls fits, reply {{"mark": -1}}.'
    )
    try:
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg: Any = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{marked_b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
    except Exception:
        msg = prompt
    # BOUNDED + vision-fallback-capable (see pick_control_by_vision). None -> caller falls back.
    resp = await _oa_llm.resilient_vlm([msg], primary=_vv._vlm())
    if resp is None:
        return None
    reply = (getattr(resp, "completion", None) or str(resp)).strip()
    _vv._VLM_CALLS["n"] += 1  # count the spend so the per-field VLM budget sees it (same as visual_check)

    mark = _parse_mark(reply)
    if mark is None or mark not in by_id:
        return None
    return by_id[mark]


# --------------------------------------------------------------------------- #
# 7. pick_option_by_marks — VISUAL OPTION COMMIT for a CUSTOM widget whose options the
#    DOM option-read MISSES (Lever styled-div radios; a custom single_select / geocomplete
#    that renders its options in a portal the delta never captures). The values are KNOWN and
#    the options are VISIBLE on screen — so we SEE them: mark the candidate option elements on
#    the screenshot (same set-of-marks as pick_control_by_marks, numbered by backend_node_id)
#    and ask the cheap VLM which marked element is the option that MEANS ``value`` (e.g. 'Yes',
#    'LinkedIn', the city). Returns the chosen NODE so the caller clicks it BY COORDINATE
#    (node center -> cdp_click_xy). GENERIC — no per-ATS string, no label/aria/data-* hook; it
#    works on ANY custom widget because it reads what is rendered, exactly as a human does.
# --------------------------------------------------------------------------- #
async def pick_option_by_marks(session: Any, value: str, candidates: list[Any], *, llm: Any = None) -> Any | None:
    """Pick the rendered OPTION element that means ``value`` by ONE set-of-marks VLM look.

    ``candidates`` are the VISIBLE clickable option elements (a styled-div radio group, the open
    custom-dropdown option cells, the geocomplete suggestion rows). Each is boxed + numbered with its
    backend_node_id on the screenshot (``create_highlighted_screenshot``, filter_highlight_ids=False);
    the VLM returns the number of the option whose visible text means ``value``. ONE screenshot + ONE
    cheap VLM call, counted through ``vision_verify``'s per-page/-field budget. Returns the chosen node,
    or None on any failure / no clear pick (caller falls back). GENERIC — pure visual option read."""
    cands = [c for c in candidates if getattr(c, "backend_node_id", None) is not None]
    if not cands:
        return None
    # Budget guard: a capped page returns no pick without a screenshot (no spend).
    if _vv._VLM_CALLS["n"] >= _vv.VLM_MAX_CALLS:
        return None
    png = await _oa_llm.bounded_screenshot(session)  # bounded — a stalled screenshot can't hang the field
    if png is None:
        return None
    import base64

    raw_b64 = base64.b64encode(png).decode()

    # Build the set-of-marks: a selector_map of ONLY the candidate OPTION elements. Each draws a
    # numbered box labeled with its backend_node_id (filter_highlight_ids=False), so the VLM's number
    # maps straight back to the node.
    by_id = {int(c.backend_node_id): c for c in cands}
    try:
        from browser_use.browser.python_highlights import create_highlighted_screenshot

        marked_b64 = await create_highlighted_screenshot(raw_b64, by_id, filter_highlight_ids=False)
    except Exception:
        return None

    legend = ", ".join(str(i) for i in by_id)
    prompt = (
        f"This is a job-application web form screenshot. Candidate selectable OPTIONS are outlined by "
        f"dashed boxes, each labeled with a NUMBER ({legend}). I want to select the option that means "
        f'"{value}". Reply STRICT JSON {{"mark": <the NUMBER of the boxed option whose visible text '
        f'means "{value}" — the matching choice / Yes-No answer / dropdown item / location suggestion>}}.'
        f' If NONE of the boxed options means "{value}", reply {{"mark": -1}}.'
    )
    try:
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg: Any = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{marked_b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
    except Exception:
        msg = prompt
    # BOUNDED + vision-fallback-capable (see pick_control_by_marks). None -> caller falls back.
    resp = await _oa_llm.resilient_vlm([msg], primary=_vv._vlm())
    if resp is None:
        return None
    reply = (getattr(resp, "completion", None) or str(resp)).strip()
    _vv._VLM_CALLS["n"] += 1  # count the spend so the per-field VLM budget sees it (same as visual_check)

    mark = _parse_mark(reply)
    if mark is None or mark not in by_id:
        return None
    return by_id[mark]


def _parse_mark(raw: str) -> int | None:
    """Tolerant parse of the VLM's {"mark": N} (or bare integer) reply -> int, or None."""
    import json
    import re as _re

    s = (raw or "").strip()
    with contextlib.suppress(Exception):
        m = _re.search(r"\{.*\}", s, _re.DOTALL)
        if m:
            obj = json.loads(m.group(0).replace("'", '"'))
            if isinstance(obj.get("mark"), int):
                return obj["mark"]
    with contextlib.suppress(Exception):
        m = _re.search(r"-?\d+", s)
        if m:
            return int(m.group(0))
    return None


def _parse_index(raw: str) -> int | None:
    """Tolerant parse of the VLM's {"index": N} reply -> int, or None."""
    import json
    import re as _re

    s = (raw or "").strip()
    with contextlib.suppress(Exception):
        m = _re.search(r"\{.*\}", s, _re.DOTALL)
        if m:
            obj = json.loads(m.group(0).replace("'", '"'))
            if isinstance(obj.get("index"), int):
                return obj["index"]
    with contextlib.suppress(Exception):
        m = _re.search(r"-?\d+", s)
        if m:
            return int(m.group(0))
    return None


def route_verdict(verdict: str) -> Verdict:
    """Pure mapping of a raw ``visual_check`` reply -> the 3-way (+UNKNOWN) ``Verdict`` (§6.1):
        matches==true                  -> CORRECT
        filled==false                  -> EMPTY    (commit never registered)
        filled==true & matches==false  -> WRONG    (committed the wrong option)
        else (capped / error / null)   -> UNKNOWN
    Split out from ``verify`` so it is unit-testable at $0 over canned verdict strings."""
    if _vv._matches(verdict):
        return "CORRECT"
    filled = _vv._is_filled(verdict)
    if not filled:
        # Either a genuine empty, OR a capped/null sentinel that also lacks "filled":true.
        # Distinguish null/capped (UNKNOWN) from a definite filled:false (EMPTY).
        low = verdict.lower().replace("'", '"').replace(" ", "")
        if '"filled":false' in low:
            return "EMPTY"
        return "UNKNOWN"
    # filled==true but matches!=true -> the field holds a different value.
    return "WRONG"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _norm_lower(s: str | None) -> str:
    return _wr.norm(s).lower()


def _dedupe_cap(items: list[str], cap: int) -> list[str]:
    """Order-preserving case-insensitive dedupe, capped at ``cap`` (§6.2 VARIANT_CAP)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = (it or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= cap:
            break
    return out


# --------------------------------------------------------------------------- #
# OFFLINE self-test — fake LLM (like wd_offline), real reuse, $0, no browser/VLM.
# Asserts: classify (incl. Gap-B UNKNOWN + deterministic overrides), variant generation
# (cap/order/prefix/dedupe), pick (reuse _llm_pick), and verify routing (CORRECT/EMPTY/WRONG/UNKNOWN).
# --------------------------------------------------------------------------- #
class _FakeCompletion:
    def __init__(self, obj: Any) -> None:
        self.completion = obj


class _FakeLLM:
    """Stands in for ChatGoogle/bu-2-0 — routes on the output_format model NAME and the prompt,
    returning a structured ``.completion`` exactly like the real ``ainvoke(..., output_format=)``."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages: Any, output_format: Any = None) -> _FakeCompletion:
        self.calls += 1
        name = getattr(output_format, "__name__", "")
        # last UserMessage content carries the label/value/value payload
        payload = ""
        for m in messages:
            payload = str(getattr(m, "content", "")) or payload
        low = payload.lower()
        if name == "_Nat":
            if "why do you want" in low or "cover letter" in low or "linkedin" in low:
                return _FakeCompletion(output_format(nature="free_text", cardinality="one"))
            if "school" in low or "university" in low or "city" in low or "skill" in low:
                card = "many" if "skill" in low else "one"
                return _FakeCompletion(output_format(nature="searchable", cardinality=card))
            if "degree" in low or "gender" in low or "state" in low:
                return _FakeCompletion(output_format(nature="closed_list", cardinality="one"))
            if "garbled_unknown_label" in low:
                return _FakeCompletion(output_format(nature="???", cardinality="one"))  # out-of-vocab
            return _FakeCompletion(output_format(nature="free_text", cardinality="one"))
        if name == "_Vars":
            if "ucla" in low or "los angeles" in low:
                return _FakeCompletion(
                    output_format(
                        variants=[
                            "University of California, Los Angeles",
                            "UCLA",
                            "California Los Angeles",
                            "Los Angeles",
                        ]
                    )
                )
            return _FakeCompletion(output_format(variants=[]))
        if name == "_Pick":
            opts = payload.split("options:")[-1]
            choice = "Bachelor's Degree" if "bachelor" in opts.lower() else "NONE"
            return _FakeCompletion(output_format(choice=choice))
        raise AssertionError(f"unexpected output_format {name!r}")


async def _selftest() -> int:
    checks: list[tuple[str, bool, Any]] = []

    def chk(name: str, passed: bool, detail: Any = "") -> None:
        checks.append((name, passed, detail))

    llm = _FakeLLM()

    # --- classify_nature ---
    n_school = await classify_nature("School or University", "UCLA", llm=llm)
    chk("classify School -> SEARCH", n_school == "SEARCH", n_school)
    n_degree = await classify_nature("Degree", "Bachelor's Degree", llm=llm)
    chk("classify Degree -> CLOSED_LIST", n_degree == "CLOSED_LIST", n_degree)
    n_why = await classify_nature("Why do you want to work here?", "Because…", llm=llm)
    chk("classify Why… -> FREE_TEXT", n_why == "FREE_TEXT", n_why)
    n_skill = await classify_nature("Skills", "Python, Go", llm=llm)
    chk("classify Skills (many) -> MULTI", n_skill == "MULTI", n_skill)

    # Gap B: out-of-vocab model reply must NOT become FREE_TEXT -> UNKNOWN.
    n_oov = await classify_nature("garbled_unknown_label", "x", llm=llm)
    chk("classify out-of-vocab -> UNKNOWN (Gap B)", n_oov == "UNKNOWN", n_oov)
    # Gap B: no LLM at all -> UNKNOWN, never a silent FREE_TEXT.
    n_nollm = await classify_nature("Why…", "x", llm=None)
    chk("classify no-llm -> UNKNOWN (Gap B)", n_nollm == "UNKNOWN", n_nollm)

    # Deterministic overrides (§4.3) win WITHOUT consuming an LLM call.
    calls_before = llm.calls
    n_opts = await classify_nature("Mystery", "x", ClassifyHints(options=["A", "B", "C"]), llm=llm)
    chk(
        "hints.options -> CLOSED_LIST (no llm call)",
        n_opts == "CLOSED_LIST" and llm.calls == calls_before,
        (n_opts, llm.calls - calls_before),
    )
    n_bool_opts = await classify_nature("Authorized?", "Yes", ClassifyHints(options=["Yes", "No"]), llm=llm)
    chk("hints.options yes/no -> BOOLEAN", n_bool_opts == "BOOLEAN", n_bool_opts)
    n_bool_val = await classify_nature("Authorized to work?", "yes", llm=llm)
    chk("yes value -> BOOLEAN (no llm)", n_bool_val == "BOOLEAN", n_bool_val)
    n_multi = await classify_nature("Languages", "x", ClassifyHints(known_multi=True), llm=llm)
    chk("hints.known_multi -> MULTI", n_multi == "MULTI", n_multi)

    # BUILD FIX C — value_is_list ALONE no longer forces MULTI (the location mis-route). A comma in a
    # single-value field's value must NOT split it across pills; only known_multi makes a field MULTI.
    n_comma_only = await classify_nature(
        "Current location", "San Francisco, CA", ClassifyHints(value_is_list=True), llm=llm
    )
    chk(
        "value_is_list alone -> NOT MULTI (location mis-route fix)",
        n_comma_only != "MULTI",
        n_comma_only,
    )
    # is_multi_label: only genuine multi labels (Skills/Languages/Technologies/Tools), never a location.
    chk(
        "is_multi_label: Skills/Languages yes, location/city no",
        is_multi_label("Technical Skills")
        and is_multi_label("Languages")
        and not is_multi_label("Current location")
        and not is_multi_label("City"),
        (is_multi_label("Technical Skills"), is_multi_label("Current location")),
    )
    chk(
        "classify return is always a Nature member",
        all(
            n in _NATURES
            for n in (n_school, n_degree, n_why, n_skill, n_oov, n_nollm, n_opts, n_bool_opts, n_bool_val, n_multi)
        ),
        True,
    )

    # --- query_variants ---
    qv = await query_variants("UCLA", "SEARCH", llm=llm)
    chk(
        "variants UCLA: canonical-first, <=3",
        qv[0] == "UCLA" and len(qv) <= _VARIANT_CAP and "University of California, Los Angeles" in qv,
        qv,
    )
    chk("variants deduped (no repeat)", len(qv) == len({q.lower() for q in qv}), qv)
    qv_text = await query_variants("Jordan Avery", "FREE_TEXT", llm=llm)
    chk("variants non-search -> [value] only", qv_text == ["Jordan Avery"], qv_text)
    qv_blank = await query_variants("", "SEARCH", llm=llm)
    chk("variants blank value -> []", qv_blank == [], qv_blank)
    qv_long = await query_variants("Massachusetts Institute of Technology", "SEARCH", llm=None)
    chk(
        "variants long no-llm -> [value, prefix] deterministic",
        qv_long[0] == "Massachusetts Institute of Technology"
        and len(qv_long) >= 2
        and qv_long[1] == "Massachusetts Institute of Technology"[:_PREFIX_LEN].strip(),
        qv_long,
    )

    # --- pick_option (reuse _llm_pick) ---
    picked = await pick_option("BS", ["Bachelor's Degree", "Master's Degree", "Doctorate"], llm=llm)
    chk("pick_option BS -> Bachelor's Degree (reuse _llm_pick)", picked == "Bachelor's Degree", picked)
    none_pick = await pick_option("Astronaut", [], llm=llm)
    chk("pick_option empty options -> None", none_pick is None, none_pick)

    # --- verify routing (pure, $0) ---
    cases = [
        ('{"filled": true, "value": "UCLA", "matches": true}', "CORRECT"),
        ('{"filled": false, "value": ""}', "EMPTY"),
        ('{"filled": true, "value": "MIT", "matches": false}', "WRONG"),
        ('{"filled": null, "capped": true}', "UNKNOWN"),
        ('{"filled": null, "error": "screenshot: boom"}', "UNKNOWN"),
    ]
    for raw, want in cases:
        got = route_verdict(raw)
        chk(f"route_verdict {want}", got == want, f"{got} <- {raw}")

    # --- DOM-first verify ORACLE (the fix): DOM read-back is PRIMARY, VLM is the AID ---
    # A counting VLM stub installed over vision_verify.visual_check so we can assert call-count.
    vlm_calls = {"n": 0}
    orig_visual_check = _vv.visual_check

    async def _counting_visual_check(session: Any, target: str, **kw: Any) -> str:
        vlm_calls["n"] += 1
        # the visual-only fake's value is visibly present
        return '{"filled": true, "value": "x", "matches": true}'

    _vv.visual_check = _counting_visual_check  # type: ignore[assignment]

    # A node + session whose live DOM value is scripted (reuse oa_dom_value's fake CDP session).
    from oa_dom_value import _FakeNode, _FakeValueSession

    fake_node = _FakeNode(7)

    # (a) DOM value == want -> CORRECT with ZERO VLM calls (the whole point).
    vlm_calls["n"] = 0
    sess_match = _FakeValueSession(value="Pyry Halonen")
    v_dom_correct = await verify(sess_match, "Full name", "Pyry Halonen", node=fake_node, llm=llm)
    chk(
        "DOM read-back match -> CORRECT, 0 VLM calls",
        v_dom_correct == "CORRECT" and vlm_calls["n"] == 0,
        (v_dom_correct, vlm_calls["n"]),
    )

    # (b) DOM value present but DIFFERENT -> WRONG, still ZERO VLM calls.
    vlm_calls["n"] = 0
    sess_wrong = _FakeValueSession(value="Someone Else")
    v_dom_wrong = await verify(sess_wrong, "Full name", "Pyry Halonen", node=fake_node, llm=llm)
    chk(
        "DOM read-back mismatch -> WRONG, 0 VLM calls",
        v_dom_wrong == "WRONG" and vlm_calls["n"] == 0,
        (v_dom_wrong, vlm_calls["n"]),
    )

    # (c) visual-only widget (DOM read empty) -> consults the VLM AID (exactly 1 call).
    vlm_calls["n"] = 0
    sess_empty = _FakeValueSession(value="")  # read_dom_value -> "" (visual-only)
    v_visual = await verify(sess_empty, "Some widget", "x", node=fake_node, llm=llm, allow_vlm=True)
    chk(
        "DOM empty -> VLM AID consulted (1 call) -> CORRECT",
        v_visual == "CORRECT" and vlm_calls["n"] == 1,
        (v_visual, vlm_calls["n"]),
    )

    # (d) DOM empty + VLM disallowed (per-field budget spent) -> EMPTY, NO VLM call.
    vlm_calls["n"] = 0
    v_nobudget = await verify(sess_empty, "Some widget", "x", node=fake_node, llm=llm, allow_vlm=False)
    chk(
        "DOM empty + budget spent -> EMPTY, 0 VLM calls",
        v_nobudget == "EMPTY" and vlm_calls["n"] == 0,
        (v_nobudget, vlm_calls["n"]),
    )

    # (e) exact normalized identity matches WITHOUT consuming an LLM call.
    calls_pre = llm.calls
    sess_exact = _FakeValueSession(value="  PYRY  halonen ")
    vlm_calls["n"] = 0
    v_exact = await verify(sess_exact, "Full name", "Pyry Halonen", node=fake_node, llm=llm)
    chk(
        "DOM exact-normal match -> CORRECT, 0 VLM + 0 LLM",
        v_exact == "CORRECT" and vlm_calls["n"] == 0 and llm.calls == calls_pre,
        (v_exact, vlm_calls["n"], llm.calls - calls_pre),
    )

    _vv.visual_check = orig_visual_check  # type: ignore[assignment]

    # --- _parse_mark (set-of-marks reply parse, pure, $0) ---
    chk("_parse_mark JSON", _parse_mark('{"mark": 642}') == 642, _parse_mark('{"mark": 642}'))
    chk("_parse_mark JSON single-quote", _parse_mark("{'mark': 7}") == 7, _parse_mark("{'mark': 7}"))
    chk("_parse_mark bare int", _parse_mark("the answer is 13") == 13, _parse_mark("the answer is 13"))
    chk("_parse_mark -1 (no fit)", _parse_mark('{"mark": -1}') == -1, _parse_mark('{"mark": -1}'))
    chk("_parse_mark garbage -> None", _parse_mark("nope") is None, _parse_mark("nope"))

    # --- pick_control_by_marks: single candidate short-circuits to it (no screenshot, no VLM) ---
    class _N:
        def __init__(self, b: int) -> None:
            self.backend_node_id = b

    one = _N(99)
    picked_one = await pick_control_by_marks(None, "Q", [one], llm=llm)
    chk("pick_control_by_marks single candidate -> that node", picked_one is one, picked_one)
    picked_none = await pick_control_by_marks(None, "Q", [], llm=llm)
    chk("pick_control_by_marks no candidates -> None", picked_none is None, picked_none)

    ok = True
    print("\n=== oa_brain offline self-test (fake llm, no browser/VLM, $0) ===")
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks, {llm.calls} fake-llm calls)")
    return 0 if ok else 1


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_selftest()))
