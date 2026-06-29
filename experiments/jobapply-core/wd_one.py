"""Run ONE live Workday job end-to-end (deterministic engine + agent backstop), fill-only, NEVER submit.
Captures a My-Experience screenshot + its own log. Args: <tenant> <host> <site> <profile_idx>.
Used by wd_multi.py to run 5 tenants x profiles in parallel (isolated process per job)."""

import asyncio
import json
import os
import secrets
import ssl
import string
import sys
import urllib.request
from pathlib import Path

BASE = "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X/.claude/worktrees/sweet-shtern-067600/experiments/jobapply-core"
sys.path.insert(0, BASE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE + "/.env")
os.environ.setdefault("GH_VERIFY_MAX_CALLS", "60")  # VLM read-back is ~$0.0006/call — verify liberally
OUT = Path(BASE) / "runs" / "wd_multi"
OUT.mkdir(parents=True, exist_ok=True)

PROFILES = [
    {"_name": "jordan", "first_name": "Jordan", "last_name": "Rivera", "email": "jordan.r.demo@example.com",
     "phone": "415-555-0142", "address": "500 Mission St", "city": "San Francisco", "state": "California",
     "postal_code": "94105", "country": "United States of America",
     "experience": [{"title": "Senior Software Engineer", "company": "Acme Corp", "location": "San Francisco, CA",
                     "summary": "Led backend distributed systems.", "start": "2021-06", "current": True},
                    {"title": "Software Engineer", "company": "Beta Labs", "location": "Austin, TX",
                     "summary": "Built REST APIs.", "start": "2018-01", "end": "2021-05"}],
     "education": [{"university": "University of California, Berkeley", "degree": "Bachelor's Degree", "major": "Computer Science", "gpa": "3.8"}],
     "skills": ["Python", "Go", "Kubernetes", "PostgreSQL"]},
    {"_name": "priya", "first_name": "Priya", "last_name": "Sharma", "email": "priya.s.demo@example.com",
     "phone": "512-555-0173", "address": "200 Congress Ave", "city": "Austin", "state": "Texas",
     "postal_code": "78701", "country": "United States of America",
     "experience": [{"title": "Machine Learning Engineer", "company": "DataWorks", "location": "Austin, TX",
                     "summary": "Trained recommendation models.", "start": "2020-03", "current": True},
                    {"title": "Data Scientist", "company": "Insight Co", "location": "Dallas, TX",
                     "summary": "Built analytics pipelines.", "start": "2017-09", "end": "2020-02"}],
     "education": [{"university": "University of Texas at Austin", "degree": "Master's Degree",
                    "major": "Electrical and Computer Engineering", "gpa": "3.9"}],
     "skills": ["Python", "TensorFlow", "SQL", "Spark"]},
    {"_name": "marcus", "first_name": "Marcus", "last_name": "Johnson", "email": "marcus.j.demo@example.com",
     "phone": "404-555-0118", "address": "100 Peachtree St", "city": "Atlanta", "state": "Georgia",
     "postal_code": "30303", "country": "United States of America",
     "experience": [{"title": "Mechanical Engineer", "company": "Lockheed", "location": "Atlanta, GA",
                     "summary": "Designed propulsion components.", "start": "2019-05", "current": True},
                    {"title": "Design Engineer", "company": "GE Aviation", "location": "Cincinnati, OH",
                     "summary": "CAD + simulation.", "start": "2016-06", "end": "2019-04"}],
     "education": [{"university": "Georgia Institute of Technology", "degree": "Bachelor's Degree",
                    "major": "Mechanical Engineering", "gpa": "3.6"}],
     "skills": ["SolidWorks", "MATLAB", "Python", "ANSYS"]},
    {"_name": "sofia", "first_name": "Sofia", "last_name": "Garcia", "email": "sofia.g.demo@example.com",
     "phone": "305-555-0190", "address": "1 Biscayne Blvd", "city": "Miami", "state": "Florida",
     "postal_code": "33132", "country": "United States of America",
     "experience": [{"title": "Financial Analyst", "company": "Citi", "location": "Miami, FL",
                     "summary": "Built forecasting models.", "start": "2021-01", "current": True},
                    {"title": "Business Analyst", "company": "Deloitte", "location": "Tampa, FL",
                     "summary": "Process optimization.", "start": "2018-07", "end": "2020-12"}],
     "education": [{"university": "University of Florida", "degree": "Bachelor's Degree",
                    "major": "Economics", "gpa": "3.7"}],
     "skills": ["Excel", "SQL", "Tableau", "Python"]},
    {"_name": "wei", "first_name": "Wei", "last_name": "Chen", "email": "wei.c.demo@example.com",
     "phone": "206-555-0155", "address": "400 Pine St", "city": "Seattle", "state": "Washington",
     "postal_code": "98101", "country": "United States of America",
     "experience": [{"title": "Research Scientist", "company": "Allen Institute", "location": "Seattle, WA",
                     "summary": "GPU-accelerated deep learning.", "start": "2020-09", "current": True},
                    {"title": "Software Engineer", "company": "Amazon", "location": "Seattle, WA",
                     "summary": "Distributed training infra.", "start": "2017-01", "end": "2020-08"}],
     "education": [{"university": "University of Washington", "degree": "Doctorate (PhD)",
                    "major": "Computer Science", "gpa": "4.0"}],
     "skills": ["C++", "CUDA", "PyTorch", "Python"]},
]

# Complete every profile: DEFAULT English as proficient (Native or Bilingual across all 5 proficiency
# axes), plus a second language for variety. Workday's Languages section is a row repeater (name + 5
# proficiency selects); the engine fills it like Education. "can add more" -> a 2nd entry per profile.
_PROF_LANG = "Native or Bilingual"
_SECOND_LANG = ["Spanish", "Hindi", "French", "Spanish", "Mandarin Chinese"]
for _i, _p in enumerate(PROFILES):
    _p.setdefault("languages", [
        {"language": "English", "comprehension": _PROF_LANG, "overall": _PROF_LANG,
         "reading": _PROF_LANG, "speaking": _PROF_LANG, "writing": _PROF_LANG},
        {"language": _SECOND_LANG[_i % len(_SECOND_LANG)], "comprehension": "Intermediate",
         "overall": "Intermediate", "reading": "Intermediate", "speaking": "Intermediate", "writing": "Intermediate"},
    ])


def fetch_job(tenant, host, site):
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "software engineer"}).encode()
    req = urllib.request.Request(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                                 data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        jp = json.loads(r.read().decode()).get("jobPostings", [])
    return [f"https://{host}/en-US/{site}" + j["externalPath"] for j in jp[:6]]


def pw():
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*-_=+"]
    c = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(12)]
    secrets.SystemRandom().shuffle(c)
    return "".join(c)


async def main():
    from ats_engine import Credentials, run_wizard
    from ats_workday import WorkdayAdapter

    tenant, host, site, pidx = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    profile = PROFILES[pidx]
    pname = profile["_name"]
    tag = f"{tenant}_{pname}"
    os.environ["WD_MYEXP_SHOT"] = str(OUT / f"{tag}_myexp.png")  # robust My-Experience filled screenshot
    os.environ["WD_MYEXP_DOM"] = str(OUT / f"{tag}_myexp.html")  # live DOM dump for offline lang/skill diagnosis
    resume = BASE + "/../../examples/resume.pdf"
    if not Path(resume).exists():
        resume = None
    print(f"=== {tag} ===", flush=True)
    try:
        urls = fetch_job(tenant, host, site)
    except Exception as exc:
        print(f"[{tag}] CXS_FETCH_FAIL {type(exc).__name__}: {exc}", flush=True)
        return
    if not urls:
        print(f"[{tag}] NO_JOBS", flush=True)
        return
    for url in urls[:3]:
        # autofillWithResume (open_form's default) — the AUTH-verified path; applyManually auth-fails.
        # The resume parser pre-fills experience rows; the engine must RESPECT those (fill gaps only).
        creds = Credentials(email=f"jobapply.test.{secrets.randbelow(99999999)}@mailinator.com", password=pw())
        print(f"[{tag}] {url.split('/job/')[-1][:50]}", flush=True)
        try:
            res = await asyncio.wait_for(
                run_wizard(WorkdayAdapter(), url=url, profile=profile, resume=resume, headless=True,
                           allow_escalation=True, screenshot_path=str(OUT / f"{tag}.png"), creds=creds),
                timeout=480)
        except TimeoutError:
            print(f"[{tag}] TIMEOUT", flush=True)
            return
        except Exception as exc:
            print(f"[{tag}] ERROR {type(exc).__name__}: {exc}", flush=True)
            continue
        st = res.get("status", "?")
        steps = res.get("steps", [])
        rep = next((s.get("repeaters") for s in steps if s.get("repeaters")), None)
        print(f"[{tag}] status={st} steps={len(steps)} cost=${res.get('cost', 0):.4f}", flush=True)
        if st in ("FILLED_TO_REVIEW", "FILLED") or any("xperience" in (s.get("name") or "") for s in steps):
            print(f"[{tag}] >>> REACHED+FILLED (screenshots: {tag}_step*.png / {tag}_review.png)", flush=True)
            return
    print(f"[{tag}] exhausted job attempts", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
    os._exit(0)
