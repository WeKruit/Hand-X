"""Per-row DONE-field count + thin-green flag: a complete=True row with few filled fields is
probably a false-green (we missed whole sections; fill_rate is blind to undiscovered fields)."""
import glob
import json
import os
import sys

d_glob = sys.argv[1] if len(sys.argv) > 1 else "runs/newats/mega4"
rows = sorted(glob.glob(d_glob + "/*.json"), key=lambda f: int(os.path.basename(f)[:-5]) if os.path.basename(f)[:-5].isdigit() else 0)
print(f"{'row':>4} {'verdict':8} {'DONE':>4} {'ESC':>3} {'SKIP':>4} {'tot':>4}  url")
susp, greens = [], 0
for f in rows:
    b = os.path.basename(f)[:-5]
    if not b.isdigit():
        continue
    n = int(b)
    d = json.load(open(f))
    c = d.get("completeness") or {}
    res = d.get("results") or []
    done = sum(1 for e in res if e.get("outcome") == "DONE")
    esc = sum(1 for e in res if e.get("outcome") == "ESCALATE")
    skip = sum(1 for e in res if e.get("outcome") == "SKIP")
    comp = c.get("complete")
    v = "COMPLETE" if comp else ("FILLED" if comp is False else "NONE")
    url = (d.get("url") or "")[:44]
    flag = ""
    if comp:
        greens += 1
        if done < 10:
            flag = "  <== THIN GREEN"
            susp.append((n, done, url))
    print(f"{n:>4} {v:8} {done:>4} {esc:>3} {skip:>4} {len(res):>4}  {url}{flag}")
print()
print(f"greens={greens}  thin-greens(<10 DONE)={len(susp)}: {[(n, dn) for n, dn, _ in susp]}")
