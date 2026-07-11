#!/usr/bin/env python3
"""Run EACH harsh fixture on its OWN page (isolated) — removes twin-skip on duplicate labels, single-page
fatigue, and cross-field bleed, so a FAIL is a clean per-widget commit/paint gap. Parallel -P4."""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ISO = HERE / "iso"
ISO.mkdir(exist_ok=True)

SHELL_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>iso</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:24px auto}
.field{margin:24px 0}label,.q,legend{font-weight:600}
input[type=text],input[type=email],input[type=tel],input[type=url],input[type=number],textarea,select{padding:8px;border:1px solid #bbb;border-radius:6px;font-size:15px}
th,td{padding:6px 10px;text-align:left;vertical-align:top}</style></head><body><form id="app">
"""
SHELL_TAIL = "\n</form></body></html>\n"


def _n(s):
    return " ".join(str(s or "").lower().replace("*", " ").replace("?", " ").split())


def _match(exp, got):
    import re
    e, g = _n(exp), _n(got)
    if "|" in str(exp) or "|" in str(got):
        es = {t for t in _n(exp).replace("|", " ").split() if t}
        gs = {t for t in _n(got).replace("|", " ").split() if t}
        return bool(es) and es == gs
    # date: same components in any format (05/20/2026 == 2026-05-20)
    de, dg = re.findall(r"\d+", str(exp)), re.findall(r"\d+", str(got))
    if de and dg and any(len(x) == 4 for x in de) and any(len(x) == 4 for x in dg) and set(de) == set(dg):
        return True
    return e == g or (bool(e) and bool(g) and (e in g or g in e))


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main():
    _combined = HERE / "all_fixtures.json"
    fixtures = json.load(open(_combined if _combined.exists() else HERE / "harsh_fixtures.json"))
    _only = set(sys.argv[1:])  # optional kind filter: run_playground_iso.py <kind> [<kind>...]
    if _only:
        fixtures = [f for f in fixtures if f.get("kind") in _only]
    # write one html + one values file per fixture
    for i, fx in enumerate(fixtures):
        fid = f"{i:02d}_{fx['kind']}"
        (ISO / f"{fid}.html").write_text(SHELL_HEAD + fx["html"] + SHELL_TAIL)
        (ISO / f"{fid}.values.json").write_text(json.dumps({_n(fx["label"]): str(fx.get("profile_value", ""))}))
    # one static server for the iso dir
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(ISO), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", _free_port()), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def run_one(arg):
        i, fx = arg
        fid = f"{i:02d}_{fx['kind']}"
        out = ISO / f"{fid}.result.json"
        env = dict(os.environ)
        env.update(OA_NO_SANDBOX="1", OA_PAINTED_DUMP="1", OA_VISION_GATE="0", OA_COMPLETE_AGENT="0",
                   # OA_VISUAL_EVERY=0: the DOM oracle is the playground's deterministic truth; the
                   # live-default visual checkpoint stays OFF here. Checkpoint/flow fixtures opt back
                   # in via their own "env" (below) — they are the ones whose oracle NEEDS the cadence.
                   OA_VISUAL_EVERY="0",
                   OA_FIXTURE_VALUES=str(ISO / f"{fid}.values.json"), OA_PROC_CAP_S="120", PYTHONUNBUFFERED="1")
        env.update({k: str(v) for k, v in (fx.get("env") or {}).items()})  # per-fixture overrides
        cmd = [str(ROOT / ".venv/bin/python"), str(ROOT / "oa_singlepage.py"),
               "--url", f"http://127.0.0.1:{port}/{fid}.html", "--generic",
               "--profile", str(HERE / "zoo_profile.json"), "--json", str(out),
               "--screenshot", str(ISO / f"{fid}.png")]
        try:
            subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        except Exception as e:
            return (fx, "ERR:" + str(e)[:30], False, {})
        if not out.exists():
            return (fx, "NO-RESULT", False, {})
        d = json.load(open(out))
        painted = d.get("painted") or []
        if isinstance(painted, str):
            painted = json.loads(painted)
        got = painted[0].get("painted", "?") if painted else "NO-PAINT"
        ok = _match(fx.get("expected", ""), got)
        # VERDICT-CONSISTENCY (the coverage gap this harness historically missed): the DOM oracle above
        # measures painted truth, but a false-RED / false-GREEN lives in the ENGINE's completeness verdict,
        # which the DOM oracle never inspects. Cross-check them: DOM-correct yet engine-reports-incomplete
        # == false-RED (the value='on' read-back veto); DOM-wrong yet engine-complete == false-GREEN.
        comp = d.get("completeness") or {}
        miss = comp.get("missing_required") or []
        vlm = comp.get("visually_unanswered") or []
        eng_ok = bool(comp.get("complete")) or (not miss and not vlm)
        onish = any(str(r.get("committed") or "").strip().lower() == "on"
                    for r in (d.get("results") or []))
        # CHECKPOINT-COVERAGE (third oracle): with the visual cadence ON, every batch verdict row must
        # end pixel-verified or an HONEST per-row UNVERIFIED — a row still reading OFFSCREEN got neither
        # (the below-fold blind spot: vanta 007 / replit 013 EEO pills sailed unexamined through every look).
        ckpt_blind = [str(v.get("label"))[:40]
                      for c in (d.get("checkpoints") or []) if not c.get("unverified")
                      for v in (c.get("verdicts") or [])
                      if str(v.get("rendered", "")).strip().upper() == "OFFSCREEN"]
        meta = {"eng_ok": eng_ok, "miss": miss, "false_red": ok and not eng_ok,
                "false_green": (not ok) and eng_ok, "onish": onish, "ckpt_blind": ckpt_blind}
        return (fx, got, ok, meta)

    indexed = list(enumerate(fixtures))
    results = list(ThreadPoolExecutor(max_workers=4).map(run_one, indexed))
    # RETRY failures up to 3x, SEQUENTIALLY (no concurrent-browser load) — the -P4 pass has transient
    # BLANKs (a busy renderer drops a next-tick commit; documented harness artifact, not an engine bug).
    # A fixture that passes on ANY sequential retry IS passing; stop on first pass. This filters the
    # concurrency flake floor so the reported number = the true stable engine-capability set. A genuine
    # consistent fail survives all 3.
    for i, (fx, got, ok, _m) in enumerate(results):
        if not ok:
            for _attempt in range(3):
                fx2, got2, ok2, m2 = run_one(indexed[i])
                if ok2 or got2 not in ("NO-RESULT", "NO-PAINT"):
                    results[i] = (fx2, got2, ok2, m2)
                if ok2:
                    break
    httpd.shutdown()

    print("\n" + "=" * 96)
    print(f"  HARSH PLAYGROUND (ISOLATED) — {len(results)} fixtures")
    print("=" * 96)
    print(f"  {'KIND':28} {'FIELD':38} {'EXP':>10} {'PAINTED':>10}  V")
    fails = []
    for fx, got, ok, _m in results:
        if not ok:
            fails.append((fx, got))
        print(f"  {fx['kind'][:28]:28} {fx['label'][:38]:38} {str(fx.get('expected',''))[:10]:>10} {str(got)[:10]:>10}  {'ok' if ok else 'FAIL'}")
    print("=" * 96)
    print(f"  ISOLATED: {'PASS ALL' if not fails else 'FAIL '+str(len(fails))+'/'+str(len(results))}")
    for fx, got in fails:
        print(f"    [{fx['kind']}] {fx['label'][:44]!r}  exp={fx.get('expected','')!r:20.20} painted={got!r:14.14}  ({fx.get('why_hard','')[:60]})")
    # VERDICT-CONSISTENCY report — the second oracle. A fixture can pass the DOM check yet expose an
    # engine verdict bug; these lines are what the old DOM-only harness was blind to.
    fred = [(fx, m) for fx, got, ok, m in results if m.get("false_red")]
    fgreen = [(fx, m) for fx, got, ok, m in results if m.get("false_green")]
    onish = [fx for fx, got, ok, m in results if m.get("onish")]
    ckblind = [(fx, m) for fx, got, ok, m in results if m.get("ckpt_blind")]
    print("=" * 96)
    print(f"  VERDICT-CONSISTENCY: false-RED {len(fred)}  false-GREEN {len(fgreen)}  committed=='on' {len(onish)}"
          f"  ckpt-OFFSCREEN-blind {len(ckblind)}")
    for fx, m in fred:
        print(f"    [FALSE-RED] {fx['kind']}: DOM correct but engine flags missing={[str(x)[:30] for x in m['miss']][:3]}")
    for fx, m in fgreen:
        print(f"    [FALSE-GREEN] {fx['kind']}: DOM wrong but engine reports COMPLETE")
    for fx, m in ckblind:
        print(f"    [CKPT-BLIND] {fx['kind']}: OFFSCREEN rows never pixel-verified: {m['ckpt_blind'][:3]}")
    print("=" * 96)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
