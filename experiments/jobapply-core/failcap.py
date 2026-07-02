"""failcap — failure/escalation capture + triage (user directive: 'on failure / escalation we
can capture it and then you investigate to improve').

Two halves:
  1. capture(session, page, tag, status, reason, extra) — called from engine failure paths at the
     moment of failure: dumps a full-page PNG + main-frame HTML + one JSONL record under
     GH_FAIL_DIR (default runs/failures/). Best-effort — a capture failure never affects the run.
  2. CLI triage — `python failcap.py triage <results.json>`: ONE cheap VLM call per non-FILLED
     record's screenshot classifies it into an actionable bucket, so 'BLOCKED' stops hiding dead
     postings, redirects and REAL fill bugs in one bag. Writes triage.jsonl next to the input.

Buckets (visual — no title-text matching, tenants rename/localize):
  APPLICATION_FORM    the form IS on screen (empty/partial)   -> OUR bug, investigate first
  NOT_FOUND           dead posting (404 / no-longer-accepting)
  CAREERS_LANDING     company careers/job-list page (redirect-tenant embed gap)
  JOB_DESCRIPTION     posting description page, form never opened
  LOGIN_OR_VERIFY     login / create-account / email-verify gate
  CAPTCHA_OR_ANTIBOT  challenge page
  BLANK               blank/white page
  OTHER               anything else (evidence says what)
"""

import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

_BUCKETS = (
    "APPLICATION_FORM|NOT_FOUND|CAREERS_LANDING|JOB_DESCRIPTION|"
    "LOGIN_OR_VERIFY|CAPTCHA_OR_ANTIBOT|BLANK|OTHER"
)

_PROMPT = (
    "You are triaging a failed automated job application. Look at this screenshot and classify "
    f"the page into EXACTLY ONE of: {_BUCKETS}.\n"
    "APPLICATION_FORM = an application form with input fields is visible (even partially filled). "
    "NOT_FOUND = page says the posting/page does not exist or is no longer accepting applications. "
    "CAREERS_LANDING = a company careers site / job LIST page (multiple jobs, search bar). "
    "JOB_DESCRIPTION = a single job's description page without a visible application form. "
    "LOGIN_OR_VERIFY = sign-in / create-account / verification-code gate. "
    "CAPTCHA_OR_ANTIBOT = CAPTCHA or bot-check challenge. BLANK = essentially empty page.\n"
    'Reply ONLY strict JSON: {"kind": "<bucket>", "evidence": "<one short phrase you saw>"}'
)


# ---------------------------------------------------------------------------
# Half 1: capture at failure time (wired into engine failure paths)
# ---------------------------------------------------------------------------
async def capture(session, page, tag: str, status: str, reason: str, extra: dict | None = None) -> None:
    """Dump PNG + HTML + one failures.jsonl line. NEVER raises."""
    with contextlib.suppress(Exception):
        out = Path(os.environ.get("GH_FAIL_DIR", "runs/failures"))
        out.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:100]
        ts = time.strftime("%m%d_%H%M%S")
        rec: dict = {"ts": ts, "tag": safe, "status": status, "reason": str(reason)[:300], "extra": extra or {}}
        with contextlib.suppress(Exception):
            rec["url"] = await page.get_url()
        with contextlib.suppress(Exception):  # full-page PNG straight over CDP — no form-clip dependency
            sid = await page.session_id
            res = await session.cdp_client.send.Page.captureScreenshot(
                params={"format": "png", "captureBeyondViewport": True}, session_id=sid
            )
            png = out / f"{ts}_{safe}.png"
            png.write_bytes(base64.b64decode(res["data"]))
            rec["png"] = str(png)
        with contextlib.suppress(Exception):
            html = await page.evaluate("() => document.documentElement.outerHTML")
            hp = out / f"{ts}_{safe}.html"
            hp.write_text(str(html)[:400_000])
            rec["html"] = str(hp)
        with contextlib.suppress(Exception):  # classify NOW while we have the artifact (one nano call)
            if rec.get("png"):
                rec["triage"] = await _classify(Path(rec["png"]).read_bytes())
        with (out / "failures.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"   [fail-capture] {safe} status={status} triage={rec.get('triage', {}).get('kind', '?')} -> {out}/")


# ---------------------------------------------------------------------------
# Half 2: VLM classify + retroactive triage CLI
# ---------------------------------------------------------------------------
async def _classify(png_bytes: bytes) -> dict:
    """One low-detail VLM call -> {kind, evidence}. {'kind':'?'} on any failure."""
    try:
        import oa_llm as _oa
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage
        from vision_verify import _vlm

        b64 = base64.b64encode(png_bytes).decode()
        msg = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=_PROMPT),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
        raw = str(await _oa.resilient_vlm([msg], primary=_vlm()) or "")
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        kind = str(d.get("kind", "?")).upper()
        return {"kind": kind if kind in _BUCKETS else "?", "evidence": str(d.get("evidence", ""))[:120]}
    except Exception as exc:
        return {"kind": "?", "evidence": f"classify failed: {exc}"[:120]}


async def _triage(results_path: str) -> None:
    rows = json.loads(Path(results_path).read_text())
    bad = [r for r in rows if str(r.get("status", "")).upper() != "FILLED"]
    print(f"triage: {len(bad)} non-FILLED of {len(rows)} in {results_path}\n")
    out = Path(results_path).with_suffix(".triage.jsonl")
    lines = []
    for r in bad:
        ss = r.get("screenshot")
        if ss and Path(ss).exists():
            t = await _classify(Path(ss).read_bytes())
        else:
            t = {"kind": "NO_ARTIFACT", "evidence": str(r.get("error", "timeout — no artifact"))[:120]}
        rec = {
            "status": r.get("status"), "adapter": r.get("adapter"), "url": r.get("url"),
            "final_url": r.get("final_url"), "screenshot": ss, **t,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
        print(f"  {t['kind']:<18} {r.get('status', '?'):<8} {(r.get('final_url') or r.get('url') or '')[:70]}")
        print(f"  {'':<18} evidence: {t['evidence']}")
    out.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "triage":
        asyncio.run(_triage(sys.argv[2]))
    else:
        print("usage: python failcap.py triage <results.json>")
