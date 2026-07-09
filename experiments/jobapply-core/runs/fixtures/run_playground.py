#!/usr/bin/env python3
"""Run the harsh playground through the real engine and assert painted==expected per fixture.
Deterministic (values injected by label via OA_FIXTURE_VALUES; painted truth read from the DOM)."""
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


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _serve(port):
    h = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(HERE), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _n(s):
    return " ".join(str(s or "").lower().replace("*", " ").replace("?", " ").split())


def _match(exp, got):
    e, g = _n(exp), _n(got)
    if "|" in str(exp) or "|" in str(got):
        es = {t for t in _n(exp).replace("|", " ").split() if t}
        gs = {t for t in _n(got).replace("|", " ").split() if t}
        return bool(es) and es == gs
    return e == g or (bool(e) and bool(g) and (e in g or g in e))


def main():
    port = _free_port(); httpd = _serve(port)
    url = f"http://127.0.0.1:{port}/playground.html"
    out = HERE / "playground_result.json"
    env = dict(os.environ)
    env.update(
        OA_NO_SANDBOX="1", OA_PAINTED_DUMP="1", OA_VISION_GATE="1", OA_COMPLETE_AGENT="0",
        OA_FIXTURE_VALUES=str(HERE / "playground_values.json"), OA_PROC_CAP_S="300", PYTHONUNBUFFERED="1",
    )
    print(f"[playground] serving {url}")
    cmd = [str(ROOT / ".venv/bin/python"), str(ROOT / "oa_singlepage.py"), "--url", url, "--generic",
           "--profile", str(HERE / "zoo_profile.json"), "--json", str(out), "--screenshot", str(HERE / "playground.png")]
    r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
    httpd.shutdown()
    if not out.exists():
        print("[playground] NO RESULT — engine crashed:\n", r.stdout[-2500:], r.stderr[-1500:]); return 2
    res = json.load(open(out))
    painted = res.get("painted") or []
    if isinstance(painted, str):
        painted = json.loads(painted)
    if not painted:
        print("[playground] NO painted dump:\n", r.stdout[-1500:]); return 2

    print("\n" + "=" * 92)
    print(f"  HARSH PLAYGROUND — {len(painted)} fixtures")
    print("=" * 92)
    print(f"  {'KIND':26} {'FIELD':40} {'EXP':>10} {'PAINTED':>10}  V")
    fails = []
    for p in painted:
        exp, got, kind = p.get("expected", ""), p.get("painted", ""), p.get("kind", "")
        ok = _match(exp, got)
        if not ok:
            fails.append((kind, p.get("label", ""), exp, got))
        print(f"  {kind[:26]:26} {str(p.get('label',''))[:40]:40} {str(exp)[:10]:>10} {str(got)[:10]:>10}  {'ok' if ok else 'FAIL'}")
    print("=" * 92)
    print(f"  PLAYGROUND {'PASS' if not fails else 'FAIL ('+str(len(fails))+'/'+str(len(painted))+')'}")
    if fails:
        print("  FAILURES (painted != expected — a real commit/paint gap):")
        for kind, lab, exp, got in fails:
            print(f"    [{kind}] {str(lab)[:46]!r}  expected={exp!r} painted={got!r}")
    print("=" * 92)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
