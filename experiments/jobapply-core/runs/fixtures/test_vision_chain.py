#!/usr/bin/env python3
"""INJECTION TEST for the vision provider chain (铁律 2 — vision must fail OVER, then fail CLOSED).

Two stages, both driven by ENV overrides (no engine monkeypatching):

  1. FAILOVER: gemini VLM primary pointed at a black-hole (nonexistent model via GH_VERIFY_MODEL,
     OA_PRIMARY unset) while the OpenAI VLM fallback stays real -> resilient_vlm must return the
     OpenAI answer, and the heartbeat must record primary=dead, fallback:openai=ok.

  2. FAIL-CLOSED (E2E): a real engine run (oa_singlepage --generic on a local fixture page with a
     committed CHOICE field) with EVERY vision provider black-holed (bogus gemini + bogus OpenAI
     VLM models; text models stay healthy so the mapper works) -> the run must yield:
       * committed choice field outcome UNVERIFIED (never DONE-green),
       * completeness.complete == False, reason == 'vision-dead', verdict == 'UNVERIFIED',
       * NEVER a COMPLETE verdict,
       * blocker is None on this zero-iframe page (captcha bool(evaluate) regression assert),
       * vision_heartbeat records dead primary/fallback/final-retry stages.

Run:  OA_NO_SANDBOX=1 .venv/bin/python runs/fixtures/test_vision_chain.py
Exit 0 = all asserts pass.
"""
import asyncio
import base64
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

# a 1x1 white PNG (the failover stage just needs ANY image payload)
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

CHECKS: list[tuple[str, bool, str]] = []


def chk(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail[:180]}")


def _load_dotenv() -> None:
    """Minimal .env loader (browser_use does this on import; the failover stage needs the keys
    BEFORE importing anything heavy)."""
    envf = ROOT / ".env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


async def stage1_failover() -> None:
    """Black-holed gemini primary -> the chain MUST answer via the real OpenAI VLM fallback."""
    import oa_llm

    from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

    # pin AFTER imports: browser_use's own load_dotenv would re-set OA_PRIMARY from .env; the chain
    # reads env at CALL time, so what matters is the state here.
    os.environ["OA_PRIMARY"] = ""  # gemini takes the primary role; openai is the fallback vendor
    os.environ["GH_VERIFY_MODEL"] = "gemini-blackhole-does-not-exist"  # the black hole
    os.environ.pop("OA_FALLBACK_VLM_MODEL", None)
    os.environ.pop("OA_FALLBACK_MODEL", None)
    os.environ.pop("OA_OPENAI_VLM_MODEL", None)

    oa_llm._CLIENT_CACHE.clear()
    oa_llm.reset_vlm_heartbeat()

    msg = UserMessage(
        content=[
            ContentPartTextParam(type="text", text='Reply STRICT JSON: {"ok": true}'),
            ContentPartImageParam(
                type="image_url",
                image_url=ImageURL(
                    url=f"data:image/png;base64,{base64.b64encode(_PNG_1PX).decode()}",
                    detail="low",
                    media_type="image/png",
                ),
            ),
        ]
    )
    res = await oa_llm.resilient_vlm([msg])
    hb = oa_llm.get_vlm_heartbeat()
    stages = [(h["provider"], h["outcome"]) for h in hb]
    chk(
        "stage1: black-holed gemini primary -> chain still answers",
        res is not None and bool((getattr(res, "completion", None) or "").strip()),
        f"completion={str(getattr(res, 'completion', None))[:60]!r}",
    )
    chk(
        "stage1: heartbeat shows primary dead then fallback:openai ok",
        len(stages) >= 2
        and stages[0][0].startswith("primary:")
        and stages[0][1] == "dead"
        and stages[1][0] == "fallback:openai"
        and stages[1][1] == "ok",
        str(stages),
    )


FIXTURE_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<style>body{font-family:sans-serif;max-width:640px;margin:30px auto}.field{margin:22px 0}label{font-weight:600}</style>
</head><body><form id=app>
<div class=field><label for=fn>First name</label><br><input id=fn name=fn type=text></div>
<div class=field><label for=em>Email</label><br><input id=em name=em type=email></div>
<div class=field><label for=auth>Are you authorized to work in the United States?</label><br>
<select id=auth name=auth><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
</form></body></html>
"""


def stage2_fail_closed() -> None:
    """EVERY vision provider black-holed -> a real engine run must be UNVERIFIED, never COMPLETE."""
    d = HERE / "_vision_chain"
    d.mkdir(exist_ok=True)
    (d / "page.html").write_text(FIXTURE_HTML)
    (d / "values.json").write_text(json.dumps({"are you authorized to work in the united states": "Yes"}))

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    out = d / "result.json"
    out.unlink(missing_ok=True)
    env = dict(os.environ)
    env.update(
        OA_NO_SANDBOX="1",
        OA_COMPLETE_AGENT="0",
        OA_PROC_CAP_S="160",
        PYTHONUNBUFFERED="1",
        OA_FIXTURE_VALUES=str(d / "values.json"),
        # the black holes — VISION ONLY (text models stay healthy so mapping/fill work):
        OA_PRIMARY="",  # gemini takes the vlm-primary role
        GH_VERIFY_MODEL="gemini-blackhole-does-not-exist",
        OA_FALLBACK_VLM_MODEL="gpt-blackhole-does-not-exist",
        OA_OPENAI_VLM_MODEL="gpt-blackhole-does-not-exist",
        # the features under test stay ON:
        OA_VISION_GATE="1",
        OA_VISUAL_EVERY="5",
    )
    cmd = [
        str(ROOT / ".venv/bin/python"), str(ROOT / "oa_singlepage.py"),
        "--url", f"http://127.0.0.1:{port}/page.html", "--generic",
        "--profile", str(HERE / "zoo_profile.json"), "--json", str(out),
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240)
    httpd.shutdown()
    if not out.exists():
        chk("stage2: engine run produced a result JSON", False, f"rc={proc.returncode} tail={proc.stdout[-300:]!r}")
        return
    res = json.loads(out.read_text())
    comp = res.get("completeness") or {}
    rows = res.get("results") or []
    choice_rows = [r for r in rows if str(r.get("type", "")).lower() in ("single_select", "select", "combobox", "boolean")]
    hb = res.get("vision_heartbeat") or []
    dead = [h for h in hb if h.get("outcome") == "dead"]
    chk(
        "stage2: committed choice field is UNVERIFIED (never green)",
        bool(choice_rows) and all(r.get("outcome") == "UNVERIFIED" for r in choice_rows),
        str([(r.get("label", "")[:30], r.get("outcome")) for r in choice_rows]),
    )
    chk("stage2: complete == False", comp.get("complete") is False, f"complete={comp.get('complete')}")
    chk("stage2: reason == 'vision-dead'", comp.get("reason") == "vision-dead", f"reason={comp.get('reason')!r}")
    chk("stage2: verdict == 'UNVERIFIED' (never COMPLETE)", comp.get("verdict") == "UNVERIFIED", f"verdict={comp.get('verdict')!r}")
    chk("stage2: outcomes counter shows UNVERIFIED >= 1", int((res.get("outcomes") or {}).get("UNVERIFIED", 0)) >= 1,
        str(res.get("outcomes")))
    chk(
        "stage2: heartbeat recorded dead chain stages (primary+fallback+final-retry)",
        len(dead) >= 3 and any(h["provider"].startswith("final-retry:") for h in dead),
        str([(h["provider"], h["outcome"]) for h in hb][:9]),
    )
    chk(
        "stage2: zero-iframe page -> blocker is None (captcha string-trap regression)",
        res.get("blocker") in (None, ""),
        f"blocker={res.get('blocker')!r} status={res.get('status')!r}",
    )
    ckpts = [c for c in (res.get("checkpoints") or []) if c.get("fields")]
    chk(
        "stage2: checkpoint batches recorded UNVERIFIED (cadence never silently skipped)",
        bool(ckpts) and all(c.get("unverified") for c in ckpts),
        str([(c.get("trigger"), c.get("unverified"), len(c.get("fields", []))) for c in ckpts]),
    )


# A clean GH-style page that carries an AMBIENT reCAPTCHA v3 badge iframe (fixed, bottom-right,
# 256x60 — what job-boards/Lever/Ashby form-protection injects on virtually every page). P4-rerun
# regression class (76/117 false NEEDS_HUMAN): the iframe sniff alone must NEVER drive the verdict —
# the badge doesn't overlap the form and there is no VLM-confirmable overlay.
BADGE_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<style>body{font-family:sans-serif}form{max-width:640px;margin:30px auto}.field{margin:22px 0}label{font-weight:600}</style>
</head><body><form id=app>
<div class=field><label for=fn>First name</label><br><input id=fn name=fn type=text></div>
<div class=field><label for=ln>Last name</label><br><input id=ln name=ln type=text></div>
<div class=field><label for=em>Email</label><br><input id=em name=em type=email></div>
</form>
<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=ambient-badge" title="reCAPTCHA"
 style="position:fixed;bottom:14px;right:14px;width:256px;height:60px;border:0"></iframe>
</body></html>
"""


def _run_engine(page_html: str, values: dict, out_name: str, extra_env: dict) -> dict | None:
    """Serve one page + run oa_singlepage --generic against it (REAL vision cadence on);
    returns the result JSON."""
    d = HERE / "_vision_chain"
    d.mkdir(exist_ok=True)
    (d / f"{out_name}.html").write_text(page_html)
    (d / f"{out_name}.values.json").write_text(json.dumps(values))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    out = d / f"{out_name}.result.json"
    out.unlink(missing_ok=True)
    env = dict(os.environ)
    env.update(OA_NO_SANDBOX="1", OA_COMPLETE_AGENT="0", OA_PROC_CAP_S="160", PYTHONUNBUFFERED="1",
               OA_FIXTURE_VALUES=str(d / f"{out_name}.values.json"), OA_VISION_GATE="1", OA_VISUAL_EVERY="5")
    env.update(extra_env)
    cmd = [str(ROOT / ".venv/bin/python"), str(ROOT / "oa_singlepage.py"),
           "--url", f"http://127.0.0.1:{port}/{out_name}.html", "--generic",
           "--profile", str(HERE / "zoo_profile.json"), "--json", str(out)]
    subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240)
    httpd.shutdown()
    return json.loads(out.read_text()) if out.exists() else None


def stage3_ambient_badge_not_needs_human() -> None:
    """P4-rerun acceptance (a): clean form + ambient corner badge iframe, real vision cadence on
    -> verdict must NOT be NEEDS_HUMAN and blocker must stay None (the sniff is a hint at most)."""
    res = _run_engine(BADGE_HTML, {}, "badge_page", {})
    if res is None:
        chk("stage3: engine run produced a result JSON", False, "no result")
        return
    comp = res.get("completeness") or {}
    chk(
        "stage3: ambient badge iframe -> verdict != NEEDS_HUMAN",
        comp.get("verdict") != "NEEDS_HUMAN" and res.get("status") != "NEEDS_HUMAN",
        f"verdict={comp.get('verdict')!r} status={res.get('status')!r}",
    )
    chk("stage3: blocker is None (sniff never sets it)", res.get("blocker") in (None, ""), f"blocker={res.get('blocker')!r}")
    chk(
        "stage3: corner badge fails the form-overlap geometry (no hint either)",
        not res.get("blocker_hint"),
        f"blocker_hint={res.get('blocker_hint')!r}",
    )


def stage4_true_overlay_still_needs_human() -> None:
    """P4-rerun acceptance (b): the miner's REAL-DOM captcha_drag_overlay fixture with cadence on
    must STILL be NEEDS_HUMAN — via the VLM overlay confirmation, not the iframe sniff."""
    fixtures = json.load(open(HERE / "all_fixtures.json"))
    fx = next((f for f in fixtures if f.get("kind") == "captcha_drag_overlay"), None)
    if fx is None:
        chk("stage4: captcha_drag_overlay fixture present", False, "missing from all_fixtures.json")
        return
    shell = "<!doctype html><html lang=en><head><meta charset=utf-8></head><body><form id=app>"
    values = {" ".join(str(fx.get("label", "")).lower().split()): str(fx.get("profile_value", ""))}
    res = _run_engine(shell + fx["html"] + "</form></body></html>", values, "captcha_overlay", {})
    if res is None:
        chk("stage4: engine run produced a result JSON", False, "no result")
        return
    comp = res.get("completeness") or {}
    overlays = [e.get("overlay") for e in res.get("checkpoints", []) if e.get("overlay")]
    chk(
        "stage4: true blocking overlay -> NEEDS_HUMAN (never COMPLETE)",
        res.get("status") == "NEEDS_HUMAN" and comp.get("verdict") == "NEEDS_HUMAN" and comp.get("complete") is False,
        f"status={res.get('status')!r} verdict={comp.get('verdict')!r} complete={comp.get('complete')}",
    )
    chk(
        "stage4: the verdict is VLM-confirmed (overlay reported), not iframe-sniffed",
        bool(overlays) or str(res.get("blocker") or "").startswith("overlay:"),
        f"blocker={res.get('blocker')!r} ckpt-overlays={overlays}",
    )


def main() -> int:
    _load_dotenv()
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("GOOGLE_API_KEY"):
        print("SKIP: needs OPENAI_API_KEY + GOOGLE_API_KEY (real fallback proof)")
        return 1
    print("=== stage 1: failover (black-holed gemini -> real OpenAI answers) ===")
    asyncio.run(stage1_failover())
    print("=== stage 2: fail-closed E2E (all vision black-holed -> UNVERIFIED, never COMPLETE) ===")
    stage2_fail_closed()
    print("=== stage 3: ambient badge iframe -> NOT NEEDS_HUMAN (P4-rerun regression) ===")
    stage3_ambient_badge_not_needs_human()
    print("=== stage 4: true blocking overlay -> STILL NEEDS_HUMAN (VLM-confirmed) ===")
    stage4_true_overlay_still_needs_human()
    ok = all(p for _, p, _ in CHECKS)
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(CHECKS)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
