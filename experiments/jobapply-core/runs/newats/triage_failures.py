"""Cluster sweep failures into root-cause buckets so fixes swarm the PATTERN, not one URL.
For each NEAR/FAIL scoreable run, pull the full per-run json and, for every missing_required /
visually_unanswered field, read its per-field ledger row (outcome + last trace step = WHERE it died:
blank->SKIP, located-miss, ESCALATE, verify-mismatch, not-discovered). Cluster by (field-semantic,
death-signature); rank by frequency. Output = the swarm worklist + a representative URL+screenshot
per cluster."""
import json, re, sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
scored = json.loads((HERE / "sweep500_scored.json").read_text())
RUNDIR = HERE / "sweep500"


def norm(s):
    return " ".join(re.sub(r"[*✱?():]", " ", str(s or "").lower()).split())


def semantic(label):
    """Coarse field-semantic bucket from a label (so 'First Name*' and 'Legal first name' cluster)."""
    l = norm(label)
    pairs = [("name", "name"), ("email", "email"), ("phone", "phone"), ("linkedin", "linkedin"),
             ("website", "url"), ("github", "url"), ("portfolio", "url"), ("location", "location"),
             ("city", "location"), ("address", "address"), ("zip", "postcode"), ("postcode", "postcode"),
             ("country", "country"), ("gender", "eeo-gender"), ("race", "eeo-race"),
             ("ethnic", "eeo-race"), ("veteran", "eeo-veteran"), ("disab", "eeo-disability"),
             ("sponsor", "work-auth"), ("authoriz", "work-auth"), ("visa", "work-auth"),
             ("salary", "comp"), ("compensat", "comp"), ("start", "availability"),
             ("notice", "availability"), ("resume", "file"), ("cover", "file"), ("cv", "file"),
             ("school", "education"), ("degree", "education"), ("education", "education"),
             ("experience", "experience"), ("company", "experience"), ("pronoun", "pronouns"),
             ("how did you hear", "source"), ("referr", "source"), ("consent", "consent"),
             ("agree", "consent"), ("acknowledg", "consent")]
    for k, v in pairs:
        if k in l:
            return v
    return l[:22] or "unknown"


def death_sig(row):
    """WHERE the field died, from its ledger row."""
    if row is None:
        return "NOT-DISCOVERED"          # required per vision/plan but no field row -> discovery gap
    tr = row.get("trace") or []
    last = tr[-1] if tr else ""
    oc = row.get("outcome") or ""
    if "blank->SKIP" in tr:
        return "BLANK-VALUE (map/inject miss)"
    if oc == "ESCALATE":
        return f"ESCALATE ({last})"
    if "no-control" in last or "locate" in last.lower():
        return f"LOCATE-MISS ({last})"
    if "verify" in last.lower() or "mismatch" in last.lower():
        return f"VERIFY-FAIL ({last})"
    return f"{oc}:{last}" or "unknown"


clusters = defaultdict(list)   # (semantic, death) -> [ {ats, company, url, field, i, png} ]
for r in scored:
    if r.get("verdict") not in ("NEAR", "FAIL"):
        continue
    rj = RUNDIR / f"{r['i']:03d}.json"
    ledger = {}
    if rj.exists():
        try:
            full = json.loads(rj.read_text())
            for x in (full.get("results") or []):
                ledger[norm(x.get("label"))] = x
        except Exception:
            pass
    bad = []
    for m in (r.get("missing_required") or []):
        bad.append(m if isinstance(m, str) else (m.get("label") if isinstance(m, dict) else str(m)))
    for v in (r.get("visually_unanswered") or []):
        bad.append(v if isinstance(v, str) else (v.get("label") if isinstance(v, dict) else str(v)))
    for field in bad:
        row = ledger.get(norm(field))
        key = (semantic(field), death_sig(row))
        clusters[key].append({"ats": r["ats"], "company": r["company"], "url": r["url"],
                              "field": field, "i": r["i"], "png": f"sweep500/{r['i']:03d}.png"})

ranked = sorted(clusters.items(), key=lambda kv: -len(kv[1]))
print(f"=== FAILURE CLUSTERS ({sum(len(v) for v in clusters.values())} bad fields across "
      f"{len([r for r in scored if r.get('verdict') in ('NEAR','FAIL')])} non-COMPLETE runs) ===\n")
for (sem, death), items in ranked:
    ex = items[0]
    ats_spread = ", ".join(sorted({i["ats"] for i in items}))
    print(f"[{len(items):>3}x] {sem:<14} | {death:<34} | ats={ats_spread}")
    print(f"        e.g. {ex['ats']}/{ex['company']} field={ex['field'][:32]!r} i={ex['i']}")
    print(f"        {ex['url'][:78]}")
out = [{"semantic": s, "death": d, "count": len(v), "examples": v[:5]} for (s, d), v in ranked]
(HERE / "failure_clusters.json").write_text(json.dumps(out, indent=1))
print(f"\nwrote {len(out)} clusters -> failure_clusters.json")
