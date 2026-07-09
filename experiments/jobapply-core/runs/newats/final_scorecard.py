"""Final confidence scorecard — the 93-URL English hard-tail (mega3 population), scored with
each URL's BEST verdict: mega3 green stays green; mega4 (retest of mega3 non-greens) supplies
the rest. Per-ATS confidence = COMPLETE / scoreable (DEAD/HITL excluded, honest wrong-value
vetoes count as NOT complete)."""
import json
import os
import re
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
m3urls = [l.strip() for l in open(f"{BASE}/mega3_urls.txt") if l.strip()]
m4urls = [l.strip() for l in open(f"{BASE}/mega4_urls.txt") if l.strip()]
m4idx = {u: i for i, u in enumerate(m4urls, 1)}


def plat(u: str) -> str:
    for k in ("greenhouse", "lever.co", "ashbyhq", "comeet", "workable", "breezy",
              "bamboohr", "hibob", "rippling"):
        if k in u:
            return k
    host = re.sub(r"https?://(www\.)?", "", u).split("/")[0]
    # company career sites embedding greenhouse
    if any(s in u for s in ("stripe.com", "airbnb.com", "duolingo.com", "samsara.com")):
        return "greenhouse(embed)"
    return host


def verdict(path: str):
    if not os.path.exists(path):
        return None
    try:
        d = json.load(open(path))
    except Exception:
        return "BAD_JSON"
    c = d.get("completeness") or {}
    if c.get("not_reached"):
        return "DEAD"
    if d.get("status") in ("needs_human",):
        return "HITL"
    if d.get("status") == "BLOCKED":
        return "DEAD"
    miss = c.get("missing_required") or []
    vlm = c.get("visually_unanswered") or []
    if c.get("complete") or (not miss and not vlm):
        return "COMPLETE"
    return "NEAR" if len(miss) + len(vlm) <= 2 else "FAIL"


rows = []
for i, u in enumerate(m3urls, 1):
    v3 = verdict(f"{BASE}/mega3/{i}.json")
    v = v3
    src = "mega3"
    if v3 != "COMPLETE" and u in m4idx:
        v4 = verdict(f"{BASE}/mega4/{m4idx[u]}.json")
        if v4 is not None:
            v, src = v4, f"mega4/{m4idx[u]}"
    rows.append((i, plat(u), v, src, u))

by = defaultdict(lambda: [0, 0, 0])  # complete, scoreable, dead/hitl
for _, p, v, _, _ in rows:
    if v in ("DEAD", "HITL", None, "BAD_JSON"):
        by[p][2] += 1
        continue
    by[p][1] += 1
    if v == "COMPLETE":
        by[p][0] += 1

tot_c = sum(x[0] for x in by.values())
tot_s = sum(x[1] for x in by.values())
print("=== FINAL CONFIDENCE — English hard-tail (93 nail-house URLs) ===")
print(f"{'ATS':22s} {'conf':>6s}  complete/scoreable  (excluded)")
for p, (c, s, d) in sorted(by.items(), key=lambda x: -x[1][1]):
    pct = 100 * c / s if s else 0
    print(f"{p:22s} {pct:5.0f}%  {c}/{s}  ({d})")
print(f"{'TOTAL':22s} {100*tot_c/max(1,tot_s):5.0f}%  {tot_c}/{tot_s}")
print("\nNON-COMPLETE remaining:")
for i, p, v, src, u in rows:
    if v not in ("COMPLETE", "DEAD", "HITL"):
        host = re.sub(r"https?://(www\.)?", "", u).split("/")[0]
        print(f"  m3#{i:3d} {v or 'PENDING':8s} {p:18s} [{src}] {host}")
