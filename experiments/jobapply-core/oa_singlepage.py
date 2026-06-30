"""oa_singlepage — fill ONE single-page ATS form (Greenhouse / Lever / Ashby) field-by-field
via the generic ``observe_act`` primitive, instead of the per-archetype ``fill_with_ladder``.

This is the PROOF harness for the observe_act state machine on real single-page forms
(no auth -> no rate-limit). It REUSES the existing pipeline verbatim for everything EXCEPT
the per-field fill:

  * field discovery   -> the adapter's ``extract`` (boards-api / posting schema -> FormField list)
  * value mapping     -> ``ats_engine.map_fields`` (the ONE structured LLM call, label -> value)
  * navigation        -> BrowserSession + ``adapter.open_form`` (iframe drill / "Enter manually")
  * form-present gate  -> ``ats_engine.form_present`` (skip the run if the form isn't reachable)
  * end screenshot    -> ``ats_engine._screenshot`` (CDP, clipped to the form)

…and swaps ONLY the fill: each discovered field becomes a ``{label,value,required}`` dict
handed to ``observe_act(session, field)``. The per-field Outcome (DONE/OTHER/SKIP/ESCALATE)
plus the state-machine trace is recorded.

HARD CONSTRAINTS honoured:
  * FILL-ONLY — never clicks Submit / Apply-final. ``observe_act`` itself never submits, and this
    runner never clicks an advance/submit control. Single-page adapters have ``is_complete()==True``
    and no ``next_step`` — there is no submit path here.
  * No secrets in CLI args — profile/resume come from files or env, never argv.
  * ``.venv/bin/python`` — the vendored browser_use import (ats_engine already does this).

Usage (example, fill-only):
    .venv/bin/python oa_singlepage.py --url https://job-boards.greenhouse.io/acme/jobs/123 \
        --profile fixtures/profiles/jordan.json --resume fixtures/resume.pdf --screenshot out.png
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import ats_engine as eng
import oa_observe_act as oa
from ats_ashby import AshbyAdapter
from ats_greenhouse import GreenhouseAdapter
from ats_lever import LeverAdapter

_ADAPTERS: list[type[eng.ATSAdapter]] = [GreenhouseAdapter, LeverAdapter, AshbyAdapter]


def pick_adapter(url: str) -> eng.ATSAdapter | None:
    """Same host-match the sweep uses (sweep.py:_pick): Greenhouse / Lever / Ashby."""
    host = (urlparse(url).hostname or "").lower()
    for cls in _ADAPTERS:
        if any(host == h or host.endswith("." + h) or h in host for h in cls.hosts):
            return cls()
    return None


@dataclass
class FieldResult:
    name: str
    label: str
    type: str
    value_src: str
    outcome: str  # DONE | OTHER | SKIP | ESCALATE
    nature: str = ""
    committed: str = ""
    trace: list[str] | None = None

    @property
    def filled(self) -> bool:
        # "filled correctly" for the proof = a DONE/OTHER terminal (committed a value);
        # ESCALATE = deterministic gap, SKIP = left blank.
        return self.outcome in (oa.DONE, oa.OTHER)


def _field_dict(field: eng.FormField, value: str, *, resume: str | None, llm: Any) -> dict[str, Any]:
    """The observe_act input for one discovered FormField. Multi-value labels (Skills/Languages,
    or a comma/semicolon-joined value) carry cardinality='many' so the state machine enters
    S_MULTI_LOOP; everything else is 'one'."""
    label = field.label or field.name
    cardinality = "one"
    multi_label = any(k in label.lower() for k in ("skill", "language", "technolog"))
    if multi_label or (field.type or "").endswith("multi_select") or ";" in (value or ""):
        cardinality = "many"
    return {
        "label": label,
        "value": value,
        "required": bool(field.required),
        "cardinality": cardinality,
        "resume": resume if field.source == "file" else None,
        "llm": llm,
    }


async def run_single_page_oa(
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool = True,
    screenshot_path: str | None = None,
) -> dict:
    """Fill a single-page ATS form via observe_act, field-by-field. Returns a result dict with the
    per-field outcomes + a fill-rate. FILL-ONLY: never submits."""
    adapter = pick_adapter(url)
    if adapter is None:
        return {"url": url, "status": "NO_ADAPTER"}

    from browser_use import BrowserProfile, BrowserSession, ChatGoogle
    from browser_use.tokens.service import TokenCost

    # step 1 — schema extract (no browser), reused verbatim.
    title, fields = await adapter.extract(url, profile)
    print(f"[oa:{adapter.__class__.__name__}] {title}  ({len(fields)} fields)")

    tc = TokenCost(include_cost=True)
    await tc.initialize()
    llm = tc.register_llm(
        ChatGoogle(
            model="gemini-3-flash-preview",
            api_key=os.environ.get("GOOGLE_API_KEY"),
            thinking_level="minimal",
        )
    )

    # step 2 — the ONE structured mapping call (label -> value), reused verbatim.
    map_rows = [f for f in fields if f.needs_map]
    mapped = await eng.map_fields(llm, map_rows, profile, title) if map_rows else {}

    # navigate + reach the form (iframe drill / "Enter manually"), reused verbatim.
    session = BrowserSession(browser_profile=BrowserProfile(headless=headless, keep_alive=True))
    await session.start()
    await session.navigate_to(url)
    await asyncio.sleep(2.5)
    page = await session.must_get_current_page()
    page = await adapter.open_form(session, page)

    result: dict[str, Any] = {
        "adapter": adapter.__class__.__name__,
        "title": title,
        "url": url,
        "fields_total": len(fields),
        "mapped": len(mapped),
        "screenshot": None,
    }

    if not await eng.form_present(adapter, page, fields):
        with contextlib.suppress(Exception):
            result["final_url"] = await page.get_url()
        if screenshot_path:
            result["screenshot"] = await eng._screenshot(session, page, screenshot_path)
        usage = await tc.get_usage_summary()
        await session.kill()
        result.update(status="BLOCKED", cost=usage.total_cost, filled=0, results=[])
        print(f"  BLOCKED — form not reachable for {adapter.__class__.__name__}")
        return result

    # step 3 — the SWAP: per-field fill via observe_act (NOT fill_with_ladder).
    per_field: list[FieldResult] = []
    t0 = time.monotonic()
    for f in fields:
        if f.source == "skip":
            continue
        value, src = eng._resolve(f, mapped, resume)
        fd = _field_dict(f, value, resume=resume, llm=llm)
        try:
            outcome = await oa.observe_act(session, fd)
        except Exception as exc:  # a single hard field must not abort the page (fill-only proof)
            outcome = oa.ESCALATE
            fd["_trace"] = [f"EXC:{type(exc).__name__}:{exc}"]
        per_field.append(
            FieldResult(
                name=f.name,
                label=f.label or f.name,
                type=f.type,
                value_src=src,
                outcome=outcome,
                nature=fd.get("_nature", ""),
                committed=fd.get("_committed", ""),
                trace=fd.get("_trace"),
            )
        )

    secs = round(time.monotonic() - t0, 1)
    usage = await tc.get_usage_summary()
    if screenshot_path:
        result["screenshot"] = await eng._screenshot(session, page, screenshot_path)
    with contextlib.suppress(Exception):
        result["final_url"] = await page.get_url()

    _print_report(adapter.__class__.__name__, title, per_field, usage, len(mapped), secs)

    fillable = [r for r in per_field if r.outcome != oa.SKIP]
    filled = [r for r in fillable if r.filled]
    result.update(
        status="FILLED",
        cost=usage.total_cost,
        secs=secs,
        outcomes={t: sum(1 for r in per_field if r.outcome == t) for t in (oa.DONE, oa.OTHER, oa.SKIP, oa.ESCALATE)},
        fill_rate=round(len(filled) / len(fillable), 3) if fillable else 0.0,
        filled=len(filled),
        results=[
            {
                "name": r.name,
                "label": r.label,
                "type": r.type,
                "src": r.value_src,
                "outcome": r.outcome,
                "nature": r.nature,
                "committed": r.committed,
                "trace": r.trace,
            }
            for r in per_field
        ],
    )

    if headless:
        await session.kill()
    else:
        print("\n  Browser left open for review (fill-only — NOT submitted). Ctrl+C to close.")
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            while True:
                await asyncio.sleep(1)
        await session.kill()
    return result


def _print_report(
    adapter_name: str, title: str, rows: list[FieldResult], usage: Any, n_mapped: int, secs: float
) -> None:
    print("\n" + "=" * 84)
    print(f"  {adapter_name.upper()} — observe_act GENERIC FILL (fill-only, NEVER submitted)")
    print(f"  {title}")
    print("=" * 84)
    print(f"  {'FIELD':<22}{'TYPE':<22}{'NATURE':<14}{'SRC':<9}{'OUTCOME':<9}")
    print("  " + "-" * 80)
    for r in rows:
        print(f"  {r.name[:21]:<22}{r.type[:21]:<22}{r.nature[:13]:<14}{r.value_src:<9}{r.outcome:<9}")
    print("  " + "-" * 80)
    counts = {t: sum(1 for r in rows if r.outcome == t) for t in (oa.DONE, oa.OTHER, oa.SKIP, oa.ESCALATE)}
    fillable = [r for r in rows if r.outcome != oa.SKIP]
    filled = [r for r in fillable if r.filled]
    rate = (len(filled) / len(fillable) * 100) if fillable else 0.0
    print(f"  fields                  : {len(rows)}")
    print(
        f"  outcomes                : DONE={counts[oa.DONE]}  OTHER={counts[oa.OTHER]}  "
        f"SKIP={counts[oa.SKIP]}  ESCALATE={counts[oa.ESCALATE]}"
    )
    print(f"  fill-rate (DONE+OTHER / non-skip) : {rate:.0f}%  ({len(filled)}/{len(fillable)})")
    print(f"  mapped by the 1 structured call   : {n_mapped}")
    print(f"  LLM calls                         : {usage.entry_count}")
    print(f"  TOTAL LLM COST                    : ${usage.total_cost:.5f}")
    print(f"  fill wall-clock                   : {secs}s")
    print("=" * 84)


def _load_profile(path: str) -> dict:
    return json.loads(open(path, encoding="utf-8").read())


def main() -> None:
    p = argparse.ArgumentParser(description="Fill ONE single-page ATS form via observe_act (FILL-ONLY, never submits)")
    p.add_argument("--url", required=True, help="Greenhouse / Lever / Ashby single-page job URL")
    p.add_argument("--profile", required=True, help="path to a profile JSON (no secrets in argv)")
    p.add_argument("--resume", default=None, help="path to a resume file for the file field")
    p.add_argument("--screenshot", default=None, help="write an end-of-fill PNG here")
    p.add_argument("--headed", action="store_true", help="run headed (default headless)")
    p.add_argument("--json", default=None, help="write the full per-field result JSON here")
    args = p.parse_args()

    profile = _load_profile(args.profile)
    res = asyncio.run(
        run_single_page_oa(
            url=args.url,
            profile=profile,
            resume=args.resume,
            headless=not args.headed,
            screenshot_path=args.screenshot,
        )
    )
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
        print(f"  wrote {args.json}")


if __name__ == "__main__":
    main()
