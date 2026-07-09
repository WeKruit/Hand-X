"""Run a fixture through the REAL mapper (no injected values) — tests map_fields answer-generation."""
import http.server, json, os, socket, subprocess, sys, threading
from pathlib import Path
HERE=Path(__file__).resolve().parent; ROOT=HERE.parent.parent
KIND=sys.argv[1]
SHELL_HEAD="<!doctype html><html><head><meta charset=utf-8></head><body>"; SHELL_TAIL="</body></html>"
fx=[f for f in json.load(open(HERE/"all_fixtures.json")) if f.get('kind')==KIND][0]
d=HERE/"_maptest"; d.mkdir(exist_ok=True)
(d/"f.html").write_text(SHELL_HEAD+fx["html"]+SHELL_TAIL)
s=socket.socket(); s.bind(("127.0.0.1",0)); port=s.getsockname()[1]; s.close()
h=lambda *a,**k: http.server.SimpleHTTPRequestHandler(*a,directory=str(d),**k)
httpd=http.server.HTTPServer(("127.0.0.1",port),h); threading.Thread(target=httpd.serve_forever,daemon=True).start()
env=dict(os.environ); env.update(OA_NO_SANDBOX="1",OA_PAINTED_DUMP="1",OA_VISION_GATE="0",OA_COMPLETE_AGENT="0",
    OA_PROC_CAP_S="120",PYTHONUNBUFFERED="1")   # NOTE: NO OA_FIXTURE_VALUES -> real mapper runs
cmd=[str(ROOT/".venv/bin/python"),str(ROOT/"oa_singlepage.py"),"--url",f"http://127.0.0.1:{port}/f.html",
     "--generic","--profile",str(HERE/"zoo_profile.json"),"--json",str(d/"r.json"),"--screenshot",str(d/"f.png")]
r=subprocess.run(cmd,cwd=str(ROOT),env=env,text=True,capture_output=True,timeout=180)
for ln in r.stdout.splitlines():
    if any(t in ln for t in ("[map]","value=","consent","agree","choose_option","s3-choice","painted","DEFAULT")): print(ln[:160])
res=json.load(open(d/"r.json")); print("PAINTED:", res.get("painted"))
