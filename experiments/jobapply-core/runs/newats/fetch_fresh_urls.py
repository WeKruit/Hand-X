"""Fetch FRESH live apply URLs off public ATS board APIs (no auth), dedup vs already-swept,
cap per company for diversity, round-robin 5 profiles. Output: fresh500.jsonl + sweep500.tsv.
Run UNSANDBOXED (needs network + certifi via httpx)."""
import json, re, sys, time
from pathlib import Path
import httpx

HERE = Path(__file__).resolve().parent           # runs/newats
CORE = HERE.parent.parent                          # jobapply-core
SLUGS = json.load(open("/tmp/slugs.json"))
H = {"User-Agent": "Mozilla/5.0"}
PER_COMPANY = 14                                   # cap so no single board dominates
TARGET = 500

# ---- already-swept URLs (dedup) ----
seen = set()
for f in ["freshmatrix_jobs.txt", "gen_urls.txt", "mega4_urls.txt", "atsx_urls.txt"]:
    p = HERE / f
    if p.exists():
        for ln in p.read_text().splitlines():
            u = ln.split("\t")[-1].strip()
            if u.startswith("http"):
                seen.add(u.split("?")[0].rstrip("/"))
try:
    for j in json.load(open(HERE / "newats_meta.json")):
        seen.add(j["apply_url"].split("?")[0].rstrip("/"))
except Exception:
    pass


def norm(u):
    return u.split("?")[0].rstrip("/")


def get(u):
    for _ in range(2):
        try:
            r = httpx.get(u, timeout=25, headers=H, follow_redirects=True)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(1)
    return None


rows = []  # {ats, company, url}


def add(ats, company, url):
    if norm(url) in seen:
        return False
    seen.add(norm(url))
    rows.append({"ats": ats, "company": company, "url": url})
    return True


# ---- Greenhouse ----
for slug in SLUGS.get("greenhouse", []):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not d:
        continue
    n = 0
    for j in d.get("jobs", []):
        au = j.get("absolute_url", "")
        if au and add("greenhouse", slug, au):
            n += 1
        if n >= PER_COMPANY:
            break
    print(f"GH {slug}: +{n}", flush=True)

# ---- Lever ----
for slug in SLUGS.get("lever", []):
    d = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        continue
    n = 0
    for j in d:
        au = j.get("applyUrl") or (j.get("hostedUrl", "") + "/apply" if j.get("hostedUrl") else "")
        if au and add("lever", slug, au):
            n += 1
        if n >= PER_COMPANY:
            break
    print(f"Lever {slug}: +{n}", flush=True)

# ---- Ashby ----
for slug in SLUGS.get("ashby", []):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(d, dict):
        continue
    n = 0
    for j in d.get("jobs", []):
        ju = j.get("jobUrl") or ""
        if not ju:
            jid = j.get("id") or ""
            ju = f"https://jobs.ashbyhq.com/{slug}/{jid}" if jid else ""
        au = (ju.rstrip("/") + "/application") if ju else ""
        if au and add("ashby", slug, au):
            n += 1
        if n >= PER_COMPANY:
            break
    print(f"Ashby {slug}: +{n}", flush=True)

print(f"\nTOTAL fresh (pre-cap): {len(rows)}", flush=True)

# ---- keep TARGET, spread across companies (round-robin by company) ----
by_co = {}
for r in rows:
    by_co.setdefault(r["company"], []).append(r)
spread = []
while len(spread) < TARGET and any(by_co.values()):
    for co in list(by_co):
        if by_co[co]:
            spread.append(by_co[co].pop(0))
        if len(spread) >= TARGET:
            break
rows = spread[:TARGET]

# ---- write corpus + round-robin profile assignment ----
PROFILES = ["fixtures/rich_profile.json", "fixtures/rich_profile2.json",
            "fixtures/profile_intl.json", "fixtures/profile_minimal.json",
            "fixtures/profile_veteran.json"]
(HERE / "fresh500.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
with (HERE / "sweep500.tsv").open("w") as f:
    for i, r in enumerate(rows):
        f.write(f"{PROFILES[i % 5]}\t{r['url']}\t{r['ats']}\t{r['company']}\n")

from collections import Counter
print("FINAL:", len(rows), "urls", dict(Counter(r["ats"] for r in rows)))
print("companies:", len(by_co))
