"""LIVE verification of the deterministic Workday repeater engine. Drives a live Intel job through
run_wizard (auth -> My Information -> My Experience -> ... -> Review, NEVER submits). At My Experience
the wired fill_deterministic runs; we print its ledger summary + screenshot. Fill-only, throwaway email."""

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
SP = Path("/private/tmp/claude-501/-Users-adam-Desktop-WeKruit-VALET---GH-Hand-X--claude-worktrees-sweet-shtern-067600/23ac21d1-d748-4d83-8a1f-9079025116b7/scratchpad")
os.environ["GH_DUMP"] = str(SP / "wd_verify_dom")

PROFILE = {
    "first_name": "Jordan", "last_name": "Rivera", "email": "jordan.rivera.demo@example.com",
    "phone": "415-555-0199", "address": "500 Mission St", "city": "San Francisco", "state": "California",
    "postal_code": "94105", "country": "United States of America",
    "experience": [
        {"title": "Senior Software Engineer", "company": "Acme Corp", "location": "San Francisco, CA",
         "summary": "Led backend services and distributed systems.", "start": "2021-06", "current": True},
        {"title": "Software Engineer", "company": "Beta Labs", "location": "Austin, TX",
         "summary": "Built REST APIs and data pipelines.", "start": "2018-01", "end": "2021-05"},
    ],
    "education": [{"university": "UC Berkeley", "degree": "BS", "major": "Computer Science", "gpa": "3.8"}],
    "skills": ["Python", "Go", "Kubernetes", "PostgreSQL"],
}


def live_intel_jobs(n=4):
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "software engineer"}).encode()
    req = urllib.request.Request("https://intel.wd1.myworkdayjobs.com/wday/cxs/intel/External/jobs",
                                 data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        jp = json.loads(r.read().decode()).get("jobPostings", [])
    return ["https://intel.wd1.myworkdayjobs.com/en-US/External" + j["externalPath"] for j in jp[:n]]


def pw():
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*-_=+"]
    c = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(12)]
    secrets.SystemRandom().shuffle(c)
    return "".join(c)


async def main():
    from ats_engine import Credentials, run_wizard

    urls = live_intel_jobs()
    print(f"live Intel jobs: {len(urls)}", flush=True)
    resume = BASE + "/../../examples/resume.pdf"
    if not Path(resume).exists():
        resume = None
    for url in urls[:2]:
        jr = url.split("_")[-1].split("/")[0]
        creds = Credentials(email=f"jobapply.test.{secrets.randbelow(99999999)}@mailinator.com", password=pw())
        print(f"\n=== [{jr}] {url.split('/job/')[-1][:60]} ===", flush=True)
        try:
            res = await asyncio.wait_for(
                run_wizard(__import__("ats_workday").WorkdayAdapter(), url=url, profile=PROFILE,
                           resume=resume, headless=True, allow_escalation=False,
                           screenshot_path=str(SP / f"wd_verify_{jr}.png"), creds=creds),
                timeout=300)
        except TimeoutError:
            print(f"[{jr}] TIMEOUT", flush=True)
            continue
        except Exception as exc:
            print(f"[{jr}] ERROR {type(exc).__name__}: {exc}", flush=True)
            continue
        rep = (res.get("repeaters") or {}).get("deterministic")
        print(f"[{jr}] status={res.get('status')} step_final={res.get('final_url','')[-40:]}", flush=True)
        print(f"[{jr}] DETERMINISTIC repeaters: {json.dumps(rep) if rep else res.get('repeaters')}", flush=True)
        if rep:
            print(f"[{jr}] >>> rounds={rep.get('rounds')} filled={rep.get('filled')} "
                  f"rows_added={rep.get('rows_added')} residual={len(rep.get('residual', []))}", flush=True)
            return
    print("no job reached My Experience", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
    os._exit(0)
