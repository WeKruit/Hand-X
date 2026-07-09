#!/usr/bin/env python3
"""Widget-zoo self-test — the finite failure-mode matrix, run OFFLINE and deterministically.

Serves widget_zoo.html locally, runs the REAL observe_act engine over it (via oa_singlepage), then
asserts each field's PAINTED ground truth (read straight from our own fixture DOM — .active button,
:checked radio, select value — NOT the VLM) against its data-expected. A pill left BLANK while its
hidden radio is checked (the AfterQuery/decoupled false-green) fails here instantly, in seconds, free.

Add a new widget = add a `.field[data-kind]` section + expected. Regressions caught forever.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # jobapply-core


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(port: int) -> http.server.HTTPServer:
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(HERE), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    port = _free_port()
    httpd = _serve(port)
    url = f"http://127.0.0.1:{port}/widget_zoo.html"
    out = HERE / "zoo_result.json"
    env = dict(os.environ)
    env.update(
        OA_NO_SANDBOX="1", OA_PAINTED_DUMP="1", OA_VISION_GATE="1", OA_COMPLETE_AGENT="0",
        OA_PROC_CAP_S="180", PYTHONUNBUFFERED="1",
    )
    print(f"[zoo] serving {url}")
    cmd = [
        str(ROOT / ".venv/bin/python"), str(ROOT / "oa_singlepage.py"),
        "--url", url, "--generic", "--profile", str(HERE / "zoo_profile.json"),
        "--json", str(out), "--screenshot", str(HERE / "zoo.png"),
    ]
    r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    httpd.shutdown()
    if not out.exists():
        print("[zoo] NO RESULT JSON — engine crashed:\n", r.stdout[-2000:], r.stderr[-2000:])
        return 2

    res = json.load(open(out))
    painted = res.get("painted") or []
    if isinstance(painted, str):
        painted = json.loads(painted)
    if not painted:
        print("[zoo] NO painted dump (OA_PAINTED_DUMP path missed):\n", r.stdout[-1500:])
        return 2

    # engine's own ledger, by normalized label, for the 3-way comparison (expected | painted | ledger)
    def _n(s):
        return " ".join(str(s or "").lower().replace("*", " ").replace("?", " ").split())
    ledger = {_n(x.get("label")): str(x.get("committed") or x.get("value") or "") for x in res.get("results", [])}

    def _match(exp, got):
        e, g = _n(exp), _n(got)
        if "|" in str(exp) or "|" in str(got):  # multi-select: order-independent set compare
            es = {t for t in _n(exp).replace("|", " ").split() if t}
            gs = {t for t in _n(got).replace("|", " ").split() if t}
            return bool(es) and es == gs
        return e == g or (e and g and (e in g or g in e))

    print("\n" + "=" * 78)
    print(f"  WIDGET ZOO — {len(painted)} fields   (painted = deterministic DOM truth)")
    print("=" * 78)
    print(f"  {'KIND':14} {'FIELD':40} {'EXP':>6} {'PAINTED':>8}  {'LEDGER':>8}  V")
    fails = 0
    for p in painted:
        exp, got, kind = p.get("expected", ""), p.get("painted", ""), p.get("kind", "")
        led = ledger.get(_n(p.get("label")), "?")
        ok = _match(exp, got)
        fails += not ok
        print(f"  {kind:14} {str(p.get('label',''))[:40]:40} {exp:>6} {got:>8}  {str(led)[:8]:>8}  {'ok' if ok else 'FAIL'}")
    print("=" * 78)
    verdict = "PASS" if not fails else f"FAIL ({fails}/{len(painted)})"
    print(f"  ZOO {verdict}   (painted != expected => a commit/paint bug the ledger may hide)")
    print("=" * 78)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
