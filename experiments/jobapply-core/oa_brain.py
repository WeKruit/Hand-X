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
    value_is_list  : the value is a comma-joined set the caller wants spread across pills -> MULTI
    """

    options: list[str] = field(default_factory=list)
    known_multi: bool = False
    value_is_list: bool = False


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
      * ``hints.known_multi`` / ``hints.value_is_list`` -> MULTI
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
    if h.known_multi or h.value_is_list:
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
        res = await llm.ainvoke(
            [SystemMessage(content=system), UserMessage(content=f"label: {label_text!r}\nvalue: {value!r}")],
            output_format=_Nat,
        )
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
        res = await llm.ainvoke(
            [SystemMessage(content=system), UserMessage(content=f"value: {value!r}")],
            output_format=_Vars,
        )
        return [s.strip() for s in (res.completion.variants or []) if s and s.strip()]
    return []


# --------------------------------------------------------------------------- #
# 3. pick_option — reuse the memoised cheap text picker (§5 MATCH).
# --------------------------------------------------------------------------- #
async def pick_option(value: str, option_texts: list[str], *, llm: Any = None) -> str | None:
    """Best-matching option text for ``value`` from the READ option list, or ``None`` if none fit.

    Thin pass-through to ``wd_repeaters._llm_pick`` — the proven, memoised-on-(value,options)
    cheap structured picker (LLM-only, no substring/regex, no vision). Returns the EXACT option
    string (so the caller can cross-check ``committed_text`` membership at verify, §6.1)."""
    if not option_texts:
        return None
    return await _wr._llm_pick(llm, value, option_texts)


# --------------------------------------------------------------------------- #
# 4. verify — value-aware VLM, 3-way routing (§6.1). Routing contract below.
# --------------------------------------------------------------------------- #
async def verify(
    session: Any,
    label_text: str,
    value: str,
    *,
    key: str | None = None,
    use_cache: bool = True,
) -> Verdict:
    """Value-aware visual read-back of one field, mapped to the §6.1 3-way verdict.

    Reuses ``vision_verify.visual_check(want=value)`` (the value-aware ~$0.0006 VLM, cached per
    (url|label|want), capped 6/page) and parses its verdict with the SAME ``_matches`` / ``_is_filled``
    the rest of the engine uses, so the oracle is identical everywhere.

    ROUTING CONTRACT (what the caller's S_VERIFY must do with each return):
        CORRECT  - field visibly shows ``value`` (or a clearly-equivalent option) -> DONE.
        EMPTY    - field reads BLANK: the commit never registered -> S_RECOMMIT
                   (re-issue the SAME commit on the resolved element, fresh verify key; NEVER a new query).
        WRONG    - field is filled but with a DIFFERENT, non-blank value -> S_REVALUE
                   (clear the wrong value, then re-search with the next UNUSED variant; capped).
        UNKNOWN  - VLM capped / errored / returned filled:null -> the caller's §6.3 policy
                   (required SEARCH/lagged -> ESCALATE, never DONE; optional -> SKIP; intrinsic/native
                   reliable-commit -> the caller may accept on its own free DOM read, not on this verdict).

    ``key`` overrides the cache identity (default: the label) so a re-commit's re-read uses a
    FRESH key (e.g. ``f"{label}:commit#2"``) and is not served the cached stale EMPTY (§6.2)."""
    verdict = await _vv.visual_check(session, label_text, want=value, key=key, use_cache=use_cache)
    return route_verdict(verdict)


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
