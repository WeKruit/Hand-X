"""oa_llm — a RESILIENT model layer for the generic observe_act brain.

THE BUG it fixes: every per-field model await in ``oa_brain`` (classify_nature, query_variants,
the _llm_pick path, pick_control_by_marks) and ``vision_verify`` (visual_check / read_options_visually)
calls ``llm.ainvoke(...)`` / ``_vlm().ainvoke(...)`` with NO inner timeout. The primary provider is
gemini (``gemini-3-flash-preview`` text, ``gemini-3.1-flash-lite`` vision). When gemini occasionally
rate-limit-STALLS, that single await hangs ~34s (only bounded far up the stack by oa_singlepage's
``wait_for(FIELD_DEADLINE+6)``). A handful of stalled cards blow the wall-clock so the run never
finishes at 99%.

THE FIX (this module): wrap EVERY per-field model await in ``asyncio.wait_for(OA_LLM_TIMEOUT)`` + ONE
retry, and on a bounded primary timeout/error fail over to a SECOND provider:

    resilient_text(messages, output_format, *, model=None, primary=None)
    resilient_vlm(messages, *, primary=None)

PRIMARY  = the caller's existing gemini ``ChatGoogle`` (passed as ``primary`` so the TokenCost wrapper
           + the configured model are preserved). When no primary is passed we build a gemini from env.
FALLBACK = provider-agnostic, chosen at CALL TIME from whatever key is present:
             OPENAI_API_KEY    -> ChatOpenAI(OA_FALLBACK_MODEL    or 'gpt-4o-mini')
             ANTHROPIC_API_KEY -> ChatAnthropic(OA_FALLBACK_MODEL or 'claude-3-5-haiku-latest')
             GROQ_API_KEY      -> ChatGroq(OA_FALLBACK_MODEL      or 'llama-3.1-8b-instant')
           VISION fallback prefers a vision-capable model
             OPENAI_API_KEY    -> ChatOpenAI(OA_FALLBACK_VLM_MODEL    or 'gpt-4o-mini')
             ANTHROPIC_API_KEY -> ChatAnthropic(OA_FALLBACK_VLM_MODEL or 'claude-3-5-haiku-latest')

INVARIANT — NEVER an unbounded await. If the primary times out and NO fallback key is configured,
``resilient_text`` returns ``None`` (the caller already escalates on ``None`` — Gap B) and
``resilient_vlm`` returns ``None`` (the caller's ``visual_check`` returns a bounded error sentinel).
The bound is the WHOLE point: a stalled gemini fails fast (OA_LLM_TIMEOUT, default 5s) instead of
hanging the field for 34s.

Keys are read from ENV AT CALL TIME (so the fallback activates the moment the user adds
OPENAI_API_KEY + OA_FALLBACK_MODEL — no restart, no code change). A small per-(provider,model,kind)
cache avoids re-constructing the fallback client on every field. The provider that actually answered
is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

# --------------------------------------------------------------------------- #
# Bounds (env-tunable). The whole point: NO per-field await is ever unbounded.
# --------------------------------------------------------------------------- #
OA_LLM_TIMEOUT = float(os.environ.get("OA_LLM_TIMEOUT", "5.0"))  # per-attempt bound (text + vlm)
OA_LLM_RETRIES = int(os.environ.get("OA_LLM_RETRIES", "1"))  # ONE retry on the PRIMARY before fallback
# The page-level field-mapping call (ats_engine.map_fields via ResilientLLM.ainvoke) is ONE large
# structured prompt (every field row + profile + JD in a single request), not a per-field call. It
# legitimately needs more wall-clock than the tight per-FIELD OA_LLM_TIMEOUT (which exists to fail a
# stalled per-field gemini fast). Binding the mapping call to 6s starved BOTH providers and crashed
# the run before the fill loop began (observed on Lever). Give the mapping call its own, larger
# budget so it is still bounded (never an unbounded await) but has room to answer.
OA_MAP_TIMEOUT = float(os.environ.get("OA_MAP_TIMEOUT", "30.0"))  # page-level mapping call bound

# Fallback model defaults (overridable per-kind via env). Only consulted when the matching key exists.
_DEFAULT_OPENAI_TEXT = "gpt-4o-mini"
_DEFAULT_ANTHROPIC_TEXT = "claude-3-5-haiku-latest"
_DEFAULT_GROQ_TEXT = "llama-3.1-8b-instant"
_DEFAULT_OPENAI_VLM = "gpt-4o-mini"
_DEFAULT_ANTHROPIC_VLM = "claude-3-5-haiku-latest"

# Bound the SCREENSHOT that feeds every VLM path too — a stalled CDP screenshot is the other
# unbounded per-field await on the card path; without this bound it could blow FIELD_DEADLINE even
# though the model call is bounded. Short, since a screenshot is a single CDP round-trip.
OA_SCREENSHOT_TIMEOUT = float(os.environ.get("OA_SCREENSHOT_TIMEOUT", "4.0"))

# Per-(provider, model, kind) client cache so the fallback client is built once, not per field.
_CLIENT_CACHE: dict[tuple[str, str, str], Any] = {}


async def bounded_screenshot(session: Any) -> bytes | None:
    """``session.take_screenshot()`` under a HARD ``asyncio.wait_for`` — a stalled CDP screenshot can
    never hang a field. Returns the PNG bytes, or None on timeout/error (the VLM caller then degrades
    to a bounded error sentinel / its DOM read, exactly as it does for any screenshot failure)."""
    try:
        return await asyncio.wait_for(session.take_screenshot(), timeout=OA_SCREENSHOT_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        _log(f"screenshot timed out >{OA_SCREENSHOT_TIMEOUT}s -> None")
        return None
    except Exception as exc:
        _log(f"screenshot error {type(exc).__name__} -> None")
        return None


def _log(msg: str) -> None:
    # Cheap, dependency-free trace (structlog isn't wired into this experiment harness). The provider
    # that answered each per-field call is observable in the run log without changing call sites.
    with contextlib.suppress(Exception):
        print(f"[oa_llm] {msg}")


# --------------------------------------------------------------------------- #
# Fallback provider construction — provider-agnostic, key-driven, read at CALL TIME.
# Returns (client, provider_name) or (None, "") when no fallback key is configured.
# --------------------------------------------------------------------------- #
def _build_fallback(kind: str) -> tuple[Any, str]:
    """Build the fallback client for ``kind`` ('text' | 'vlm') from whatever provider key is present.

    Order: OpenAI -> Anthropic -> Groq (Groq has no vision, so it is text-only). Reads keys from env
    NOW so adding OPENAI_API_KEY lights up the fallback without a restart. Cached per
    (provider, model, kind). Returns (None, '') when NO fallback key exists (caller stays bounded)."""
    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GH_ANTHROPIC_API_KEY")
    groq_key = os.environ.get("GROQ_API_KEY")

    # OA_FALLBACK_MODEL is the generic override; OA_FALLBACK_VLM_MODEL narrows the vision fallback.
    text_model = os.environ.get("OA_FALLBACK_MODEL")
    vlm_model = os.environ.get("OA_FALLBACK_VLM_MODEL") or os.environ.get("OA_FALLBACK_MODEL")

    if openai_key:
        model = (vlm_model or _DEFAULT_OPENAI_VLM) if kind == "vlm" else (text_model or _DEFAULT_OPENAI_TEXT)
        return _cached("openai", model, kind, lambda: _mk_openai(model, openai_key)), "openai"
    if anthropic_key:
        model = (vlm_model or _DEFAULT_ANTHROPIC_VLM) if kind == "vlm" else (text_model or _DEFAULT_ANTHROPIC_TEXT)
        return _cached("anthropic", model, kind, lambda: _mk_anthropic(model, anthropic_key)), "anthropic"
    if groq_key and kind == "text":  # Groq has no vision -> only a text fallback
        model = text_model or _DEFAULT_GROQ_TEXT
        return _cached("groq", model, kind, lambda: _mk_groq(model, groq_key)), "groq"
    return None, ""


def _cached(provider: str, model: str, kind: str, build: Any) -> Any:
    key = (provider, model, kind)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = build()
        _CLIENT_CACHE[key] = client
    return client


def _mk_openai(model: str, api_key: str) -> Any:
    from browser_use.llm.openai.chat import ChatOpenAI

    return ChatOpenAI(model=model, api_key=api_key)


def _mk_anthropic(model: str, api_key: str) -> Any:
    from browser_use.llm.anthropic.chat import ChatAnthropic

    return ChatAnthropic(model=model, api_key=api_key)


def _mk_groq(model: str, api_key: str) -> Any:
    from browser_use.llm.groq.chat import ChatGroq

    return ChatGroq(model=model, api_key=api_key)


def _build_primary_text(model: str | None) -> Any | None:
    """Build a gemini text primary from env when the caller passed no ``primary`` (the brain always
    passes one — this is only for direct/standalone use). Returns None when GOOGLE_API_KEY is absent."""
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    from browser_use.llm.google.chat import ChatGoogle

    return ChatGoogle(model=model or os.environ.get("OA_BRAIN_MODEL", "gemini-3-flash-preview"), api_key=key)


# --------------------------------------------------------------------------- #
# resilient_text — bounded primary + ONE retry, then provider-agnostic fallback.
# --------------------------------------------------------------------------- #
async def resilient_text(
    messages: Any,
    output_format: Any = None,
    *,
    model: str | None = None,
    primary: Any = None,
    timeout: float | None = None,
) -> Any:
    """Bounded structured TEXT call. PRIMARY = ``primary`` (the caller's TokenCost-wrapped gemini) or
    a gemini built from env; wrapped in ``wait_for(OA_LLM_TIMEOUT)`` + ``OA_LLM_RETRIES`` retries. On a
    bounded primary timeout/error, fail over to the configured fallback provider (same
    ``output_format`` contract). Returns the provider's ``ChatInvokeCompletion`` (``.completion`` holds
    the structured object), or ``None`` when the primary is bounded-out AND no fallback key exists.

    NEVER an unbounded await: every ``ainvoke`` here is inside ``asyncio.wait_for``. ``None`` is the
    caller's escalate signal (Gap B) — it is returned ONLY after the primary was bounded, never by
    waiting."""
    prim = primary if primary is not None else _build_primary_text(model)

    # --- bounded PRIMARY (gemini) + ONE retry ---
    if prim is not None:
        res = await _bounded_invoke(prim, messages, output_format, who="gemini(primary)", timeout=timeout)
        if res is not None:
            return res

    # --- provider-agnostic FALLBACK (bounded too) ---
    fb, name = _build_fallback("text")
    if fb is None:
        # No fallback key: stay bounded. Return None -> caller escalates (Gap B), never a hang.
        _log("text: primary bounded-out, NO fallback key -> None (caller escalates)")
        return None
    res = await _bounded_invoke(fb, messages, output_format, who=f"{name}(fallback)", timeout=timeout)
    if res is None:
        _log(f"text: fallback {name} also bounded-out -> None (caller escalates)")
    return res


async def _bounded_invoke(client: Any, messages: Any, output_format: Any, *, who: str, timeout: float | None = None) -> Any:
    """ONE provider, ``OA_LLM_RETRIES + 1`` bounded attempts. Each attempt is
    ``asyncio.wait_for(timeout or OA_LLM_TIMEOUT)``. Returns the completion, or None when every attempt
    timed out / errored. NEVER raises, NEVER waits unbounded. ``timeout`` lets a page-level call (the
    big mapping request) use a larger budget than the tight per-FIELD default."""
    bound = timeout if timeout is not None else OA_LLM_TIMEOUT
    attempts = OA_LLM_RETRIES + 1
    for i in range(attempts):
        try:
            if output_format is not None:
                coro = client.ainvoke(messages, output_format=output_format)
            else:
                coro = client.ainvoke(messages)
            res = await asyncio.wait_for(coro, timeout=bound)
            if i > 0 or "fallback" in who:
                _log(f"answered by {who} (attempt {i + 1}/{attempts})")
            return res
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 — explicit on both for older runtimes
            _log(f"{who} timed out >{bound}s (attempt {i + 1}/{attempts})")
        except Exception as exc:
            _log(f"{who} error {type(exc).__name__} (attempt {i + 1}/{attempts})")
    return None


# --------------------------------------------------------------------------- #
# resilient_vlm — same bounded-primary-then-fallback pattern for the VISION call.
# --------------------------------------------------------------------------- #
async def resilient_vlm(messages: Any, *, primary: Any = None) -> Any:
    """Bounded VISION call (a ``UserMessage`` carrying image + prompt). PRIMARY = ``primary`` (the
    caller's gemini flash-lite vision client) or one built from env; bounded + retried exactly like
    ``resilient_text``. On a bounded primary timeout/error, fail over to a vision-capable fallback
    (ChatOpenAI / ChatAnthropic) when keyed. Returns the provider response (``.completion`` holds the
    text), or ``None`` when bounded-out and no vision fallback key exists (caller -> bounded error
    sentinel). NEVER an unbounded await."""
    prim = primary if primary is not None else _build_primary_vlm()
    if prim is not None:
        res = await _bounded_invoke(prim, messages, None, who="gemini(vlm-primary)")
        if res is not None:
            return res

    fb, name = _build_fallback("vlm")
    if fb is None:
        _log("vlm: primary bounded-out, NO vision fallback key -> None")
        return None
    res = await _bounded_invoke(fb, messages, None, who=f"{name}(vlm-fallback)")
    if res is None:
        _log(f"vlm: fallback {name} also bounded-out -> None")
    return res


def _build_primary_vlm() -> Any | None:
    """Build the gemini vision primary from env (mirrors vision_verify._vlm). Returns None when
    GOOGLE_API_KEY is absent — vision_verify normally passes its own client as ``primary``."""
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    from browser_use.llm.google.chat import ChatGoogle

    return ChatGoogle(model=os.environ.get("GH_VERIFY_MODEL", "gemini-3.1-flash-lite"), api_key=key)


class ResilientLLM:
    """Wrap a primary (gemini) client so EVERY ``.ainvoke`` is bounded + falls over to the configured
    fallback provider — for call sites that invoke ``llm.ainvoke`` DIRECTLY (e.g. ``ats_engine.map_fields``,
    the one structured label->value mapping call) without per-site changes. A gemini 503/"high demand" or
    a stall on such a raw call would otherwise propagate and KILL the whole run (observed on Lever/Ashby);
    routed through here it fails over to gpt-5.4-mini instead. Other attribute access proxies to the inner
    client so TokenCost / model-name introspection still work. Pass this ONLY to the raw-ainvoke sites; the
    oa_brain/vision_verify sites already call resilient_text/resilient_vlm and must keep the PLAIN client
    (double-wrapping would nest the timeouts)."""

    def __init__(self, primary: Any) -> None:
        object.__setattr__(self, "_inner", primary)

    async def ainvoke(self, messages: Any, output_format: Any = None) -> Any:
        # Page-level mapping call — ONE big structured request; use the larger OA_MAP_TIMEOUT budget,
        # not the tight per-FIELD OA_LLM_TIMEOUT, so a legitimately slow batch answer is not bounded-out.
        res = await resilient_text(messages, output_format, primary=self._inner, timeout=OA_MAP_TIMEOUT)
        if res is None:
            # primary AND fallback both unavailable: a single page-level mapping call must not silently
            # return None (callers read res.completion) — surface a clear, catchable error.
            raise RuntimeError("oa_llm.ResilientLLM: primary + fallback both unavailable")
        return res

    def __getattr__(self, name: str) -> Any:  # proxy everything else to the wrapped client
        return getattr(object.__getattribute__(self, "_inner"), name)


# --------------------------------------------------------------------------- #
# OFFLINE self-test — fake 'gemini' (stalls) + fake 'openai' (answers), $0, no network.
# Asserts: (1) a stalled primary fails fast and the fallback answers; (2) with NO fallback key the
# primary is bounded and resilient_text returns None (no hang); (3) a healthy primary is used and the
# fallback is never built; (4) resilient_vlm fails over the same way.
# --------------------------------------------------------------------------- #
class _FakeCompletion:
    def __init__(self, obj: Any) -> None:
        self.completion = obj


class _StallLLM:
    """A primary that NEVER returns within the timeout (simulates a rate-limit stall)."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages: Any, output_format: Any = None) -> Any:
        self.calls += 1
        await asyncio.sleep(60)  # far past OA_LLM_TIMEOUT — wait_for must cancel this
        return _FakeCompletion("never")


class _FastLLM:
    """A provider that answers immediately with a structured completion."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls = 0

    async def ainvoke(self, messages: Any, output_format: Any = None) -> Any:
        self.calls += 1
        obj = output_format(nature="searchable", cardinality="one") if output_format is not None else self.tag
        return _FakeCompletion(obj)


async def _selftest() -> int:
    global OA_LLM_TIMEOUT
    import sys

    from pydantic import BaseModel

    checks: list[tuple[str, bool, Any]] = []

    def chk(name: str, passed: bool, detail: Any = "") -> None:
        checks.append((name, passed, detail))

    class _Nat(BaseModel):
        nature: str
        cardinality: str = "one"

    # shrink the bound so the stall test is fast.
    OA_LLM_TIMEOUT = 0.2
    _CLIENT_CACHE.clear()

    # (1) stalled primary + fake 'openai' fallback -> the fallback answers, FAST (bounded, not 60s).
    fake_fb = _FastLLM("openai-answer")
    orig_build = globals()["_build_fallback"]
    globals()["_build_fallback"] = lambda kind: (fake_fb, "openai")  # type: ignore[assignment]
    stall = _StallLLM()
    import time as _t

    t0 = _t.monotonic()
    res = await resilient_text([], _Nat, primary=stall)
    elapsed = _t.monotonic() - t0
    chk(
        "stalled primary -> fallback answers, bounded fast",
        res is not None and res.completion.nature == "searchable" and fake_fb.calls == 1 and elapsed < 2.0,
        (None if res is None else res.completion.nature, fake_fb.calls, round(elapsed, 3)),
    )
    chk(
        "primary attempted OA_LLM_RETRIES+1 times then cancelled",
        stall.calls == OA_LLM_RETRIES + 1,
        (stall.calls, OA_LLM_RETRIES + 1),
    )

    # (2) NO fallback key -> primary bounded, returns None (NO hang), still fast.
    globals()["_build_fallback"] = lambda kind: (None, "")  # type: ignore[assignment]
    stall2 = _StallLLM()
    t0 = _t.monotonic()
    res2 = await resilient_text([], _Nat, primary=stall2)
    elapsed2 = _t.monotonic() - t0
    chk(
        "no fallback key -> None, bounded (no hang)",
        res2 is None and elapsed2 < 2.0,
        (res2, round(elapsed2, 3)),
    )

    # (3) healthy primary -> used directly, fallback NEVER built.
    built = {"n": 0}

    def _counting_build(kind: str) -> tuple[Any, str]:
        built["n"] += 1
        return _FastLLM("should-not-be-used"), "openai"

    globals()["_build_fallback"] = _counting_build  # type: ignore[assignment]
    healthy = _FastLLM("gemini-answer")
    res3 = await resilient_text([], _Nat, primary=healthy)
    chk(
        "healthy primary -> used, fallback NOT built",
        res3 is not None and res3.completion.nature == "searchable" and built["n"] == 0 and healthy.calls == 1,
        (built["n"], healthy.calls),
    )

    # (4) resilient_vlm: stalled vision primary -> vision fallback answers, bounded.
    fake_vfb = _FastLLM("vlm-fallback-answer")
    globals()["_build_fallback"] = lambda kind: (fake_vfb, "openai")  # type: ignore[assignment]
    vstall = _StallLLM()
    t0 = _t.monotonic()
    vres = await resilient_vlm([], primary=vstall)
    velapsed = _t.monotonic() - t0
    chk(
        "resilient_vlm: stalled primary -> fallback, bounded",
        vres is not None and vres.completion == "vlm-fallback-answer" and velapsed < 2.0,
        (None if vres is None else vres.completion, round(velapsed, 3)),
    )

    # (5) resilient_vlm: no fallback -> None, bounded.
    globals()["_build_fallback"] = lambda kind: (None, "")  # type: ignore[assignment]
    vstall2 = _StallLLM()
    t0 = _t.monotonic()
    vres2 = await resilient_vlm([], primary=vstall2)
    velapsed2 = _t.monotonic() - t0
    chk("resilient_vlm: no fallback -> None, bounded", vres2 is None and velapsed2 < 2.0, (vres2, round(velapsed2, 3)))

    globals()["_build_fallback"] = orig_build  # type: ignore[assignment]

    # (6) _build_fallback is provider-agnostic + reads env at call time. No keys -> (None, '').
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_ANTHROPIC_API_KEY", "GROQ_API_KEY"):
        os.environ.pop(k, None)
    _CLIENT_CACHE.clear()
    fb_none, name_none = _build_fallback("text")
    chk("no keys -> no fallback (None, '')", fb_none is None and name_none == "", (fb_none, name_none))

    # adding OPENAI_API_KEY lights up the openai fallback at CALL time (no restart).
    os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
    _CLIENT_CACHE.clear()
    try:
        fb_oa, name_oa = _build_fallback("text")
        chk("OPENAI_API_KEY present -> openai fallback", name_oa == "openai" and fb_oa is not None, name_oa)
    except Exception as exc:
        chk("OPENAI_API_KEY present -> openai fallback", False, f"{type(exc).__name__}: {exc}")
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
        _CLIENT_CACHE.clear()

    ok = True
    print("\n=== oa_llm offline self-test (fake providers, no network, $0) ===")
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(_selftest())
