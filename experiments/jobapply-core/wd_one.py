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

BASE = str(Path(__file__).resolve().parent)  # this worktree's jobapply-core (was hardcoded to another worktree)
sys.path.insert(0, BASE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE + "/.env")
os.environ.setdefault("GH_VERIFY_MAX_CALLS", "60")  # VLM read-back is ~$0.0006/call — verify liberally
OUT = Path(BASE) / "runs" / "wd_multi"
OUT.mkdir(parents=True, exist_ok=True)

PROFILES = [
    {
        "_name": "jordan",
        "first_name": "Jordan",
        "last_name": "Rivera",
        "email": "jordan.reyes.demo2047@gmail.com",
        "phone": "415-555-0142",
        "address": "500 Mission St",
        "city": "San Francisco",
        "state": "California",
        "postal_code": "94105",
        "country": "United States of America",
        "how_did_you_hear": "LinkedIn",
        "linkedin": "https://www.linkedin.com/in/jordan-rivera-demo/",
        # EEO / demographic / visa — ALL profile-driven. The engine fills each from these values; it
        # DECLINES only a field the profile genuinely leaves empty, and NEVER guesses. (Directive: EEO
        # incl. sexual orientation, and visa/work-authorization, come from the user profile.)
        "gender": "Male",
        "race_ethnicity": "Asian",
        "hispanic_or_latino": "No",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "No, I do not have a disability",
        "sexual_orientation": "Heterosexual",
        "gender_identity": "Cisgender",
        "transgender": "No",
        "pronoun": "He/Him",
        # visa / work authorization
        "work_authorization": "Authorized to work in the US",
        "authorized_to_work_us": "Yes",
        "requires_sponsorship": "No",
        "visa_status": "U.S. Citizen",
        "citizenship": "United States",
        # other common screening (profile-driven; neutral defaults handled by the engine when absent)
        "willing_to_relocate": "Yes",
        "salary_expectation": "180000",
        "notice_period": "2 weeks",
        "security_clearance": "None",
        "criminal_history": "No",
        "experience": [
            {
                "title": "Senior Software Engineer",
                "company": "Acme Corp",
                "location": "San Francisco, CA",
                "summary": "Led backend distributed systems.",
                "start": "2021-06",
                "current": True,
            },
            {
                "title": "Software Engineer",
                "company": "Beta Labs",
                "location": "Austin, TX",
                "summary": "Built REST APIs.",
                "start": "2018-01",
                "end": "2021-05",
            },
        ],
        "education": [
            {
                "university": "University of California, Berkeley",
                "degree": "Bachelor's Degree",
                "major": "Computer Science",
                "gpa": "3.8",
            }
        ],
        "skills": ["Python", "Go", "Kubernetes", "PostgreSQL"],
    },
    {
        "_name": "priya",
        "linkedin": "https://www.linkedin.com/in/priya-sharma-demo/",
        "first_name": "Priya",
        "last_name": "Sharma",
        "email": "priya.sharma.demo3184@gmail.com",
        "phone": "512-555-0173",
        "address": "200 Congress Ave",
        "city": "Austin",
        "state": "Texas",
        "postal_code": "78701",
        "country": "United States of America",
        "experience": [
            {
                "title": "Machine Learning Engineer",
                "company": "DataWorks",
                "location": "Austin, TX",
                "summary": "Trained recommendation models.",
                "start": "2020-03",
                "current": True,
            },
            {
                "title": "Data Scientist",
                "company": "Insight Co",
                "location": "Dallas, TX",
                "summary": "Built analytics pipelines.",
                "start": "2017-09",
                "end": "2020-02",
            },
        ],
        "education": [
            {
                "university": "University of Texas at Austin",
                "degree": "Master's Degree",
                "major": "Electrical and Computer Engineering",
                "gpa": "3.9",
            }
        ],
        "skills": ["Python", "TensorFlow", "SQL", "Spark"],
    },
    {
        "_name": "marcus",
        "linkedin": "https://www.linkedin.com/in/marcus-johnson-demo/",
        "first_name": "Marcus",
        "last_name": "Johnson",
        "email": "marcus.johnson.demo5926@gmail.com",
        "phone": "404-555-0118",
        "address": "100 Peachtree St",
        "city": "Atlanta",
        "state": "Georgia",
        "postal_code": "30303",
        "country": "United States of America",
        "experience": [
            {
                "title": "Mechanical Engineer",
                "company": "Lockheed",
                "location": "Atlanta, GA",
                "summary": "Designed propulsion components.",
                "start": "2019-05",
                "current": True,
            },
            {
                "title": "Design Engineer",
                "company": "GE Aviation",
                "location": "Cincinnati, OH",
                "summary": "CAD + simulation.",
                "start": "2016-06",
                "end": "2019-04",
            },
        ],
        "education": [
            {
                "university": "Georgia Institute of Technology",
                "degree": "Bachelor's Degree",
                "major": "Mechanical Engineering",
                "gpa": "3.6",
            }
        ],
        "skills": ["SolidWorks", "MATLAB", "Python", "ANSYS"],
    },
    {
        "_name": "sofia",
        "linkedin": "https://www.linkedin.com/in/sofia-martinez-demo/",
        "first_name": "Sofia",
        "last_name": "Garcia",
        "email": "sofia.garcia.demo7413@gmail.com",
        "phone": "305-555-0190",
        "address": "1 Biscayne Blvd",
        "city": "Miami",
        "state": "Florida",
        "postal_code": "33132",
        "country": "United States of America",
        "experience": [
            {
                "title": "Financial Analyst",
                "company": "Citi",
                "location": "Miami, FL",
                "summary": "Built forecasting models.",
                "start": "2021-01",
                "current": True,
            },
            {
                "title": "Business Analyst",
                "company": "Deloitte",
                "location": "Tampa, FL",
                "summary": "Process optimization.",
                "start": "2018-07",
                "end": "2020-12",
            },
        ],
        "education": [
            {"university": "University of Florida", "degree": "Bachelor's Degree", "major": "Economics", "gpa": "3.7"}
        ],
        "skills": ["Excel", "SQL", "Tableau", "Python"],
    },
    {
        "_name": "wei",
        "linkedin": "https://www.linkedin.com/in/wei-chen-demo/",
        "first_name": "Wei",
        "last_name": "Chen",
        "email": "wei.chen.demo8652@gmail.com",
        "phone": "206-555-0155",
        "address": "400 Pine St",
        "city": "Seattle",
        "state": "Washington",
        "postal_code": "98101",
        "country": "United States of America",
        "experience": [
            {
                "title": "Research Scientist",
                "company": "Allen Institute",
                "location": "Seattle, WA",
                "summary": "GPU-accelerated deep learning.",
                "start": "2020-09",
                "current": True,
            },
            {
                "title": "Software Engineer",
                "company": "Amazon",
                "location": "Seattle, WA",
                "summary": "Distributed training infra.",
                "start": "2017-01",
                "end": "2020-08",
            },
        ],
        "education": [
            {
                "university": "University of Washington",
                "degree": "Doctorate (PhD)",
                "major": "Computer Science",
                "gpa": "4.0",
            }
        ],
        "skills": ["C++", "CUDA", "PyTorch", "Python"],
    },
]

# NOTE: languages are NOT set on the test profiles — the ENGINE defaults English-proficient when a
# Languages section is present and the profile names no languages (wd_repeaters._plan_skeleton). A
# profile that DOES list languages overrides the default. (Per spec: "default English proficient unless
# added specifically.")


def fetch_job(tenant, host, site):
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "software engineer"}).encode()
    req = urllib.request.Request(
        f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        jp = json.loads(r.read().decode()).get("jobPostings", [])
    return [f"https://{host}/en-US/{site}" + j["externalPath"] for j in jp[:6]]


def pw():
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*-_=+"]
    c = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(12)]
    secrets.SystemRandom().shuffle(c)
    return "".join(c)


_CREDS_FILE = OUT / "wd_creds.json"


def creds_for(tenant):
    """REUSE one throwaway account PER TENANT: create it once, then SIGN IN on every later run. Creating
    a NEW account each run is exactly what trips Workday's Create-Account rate-limit / CAPTCHA (the
    AUTH_FAILED we hit on Intel/Salesforce/HP after ~15-20 accounts/day). authenticate() already does
    create-vs-sign-in (by verifyPassword), so reusing stored creds makes a repeat run SIGN IN."""
    from ats_engine import Credentials

    store = {}
    if _CREDS_FILE.exists():
        try:
            store = json.loads(_CREDS_FILE.read_text())
        except Exception:
            store = {}
    if tenant in store:
        # REUSE a tracked account -> SIGN IN (existing=True), never re-create (Workday rate-limits creates).
        return Credentials(email=store[tenant]["email"], password=store[tenant]["password"], existing=True)
    c = {
        "email": f"jobapply.test.{secrets.randbelow(99999999)}@mailinator.com",
        "password": os.environ.get("WD_PASSWORD") or pw(),
    }
    store[tenant] = c
    import contextlib

    with contextlib.suppress(Exception):
        _CREDS_FILE.write_text(json.dumps(store, indent=2))
    return Credentials(email=c["email"], password=c["password"], existing=False)


def rotate_creds(tenant):
    """A tracked account failed to SIGN IN (deleted / wrong password) — mint a FRESH one, OVERWRITE the
    store (keep tracking authoritative), and return it as a CREATE (existing=False)."""
    import contextlib

    from ats_engine import Credentials

    store = {}
    if _CREDS_FILE.exists():
        with contextlib.suppress(Exception):
            store = json.loads(_CREDS_FILE.read_text())
    c = {
        "email": f"jobapply.test.{secrets.randbelow(99999999)}@mailinator.com",
        "password": os.environ.get("WD_PASSWORD") or pw(),
    }
    store[tenant] = c
    with contextlib.suppress(Exception):
        _CREDS_FILE.write_text(json.dumps(store, indent=2))
    return Credentials(email=c["email"], password=c["password"], existing=False)


async def main():
    from ats_engine import run_wizard
    from ats_workday import WorkdayAdapter

    tenant, host, site, pidx = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    profile = PROFILES[pidx]
    pname = profile["_name"]
    tag = f"{tenant}_{pname}"
    os.environ["WD_MYEXP_SHOT"] = str(OUT / f"{tag}_myexp.png")  # robust My-Experience filled screenshot
    os.environ["WD_MYEXP_DOM"] = str(OUT / f"{tag}_myexp.html")  # live DOM dump for offline lang/skill diagnosis
    # PER-PROFILE resume (gen_resumes.py): resume and profile are ONE identity, so Workday's
    # autofill pre-fills the SAME values L1 would fill — no more rival truth sources (the
    # Chantilly-VA-vs-Miami postal clash, Spencer's phone in Marcus's app). Fallback = shared.
    resume = BASE + f"/fixtures/resumes/{pname}.pdf"
    if not Path(resume).exists():
        resume = BASE + "/../../examples/resume.pdf"
    if not Path(resume).exists():
        resume = BASE + "/fixtures/test_resume.pdf"
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

    async def _attempt(url, creds):
        # 1400s/URL: My Experience (experience+education+skills+languages) is agent-heavy and runs ~15 min
        # alone — the old 780s timed out mid-education so NO tenant reached Review. seq10.sh caps the tenant.
        return await asyncio.wait_for(
            run_wizard(
                WorkdayAdapter(),
                url=url,
                profile=profile,
                resume=resume,
                headless=True,
                allow_escalation=True,
                screenshot_path=str(OUT / f"{tag}.png"),
                creds=creds,
            ),
            timeout=1400,
        )

    for url in urls[:3]:
        # autofillWithResume (open_form's default) — the AUTH-verified path; applyManually auth-fails.
        # The resume parser pre-fills experience rows; the engine must RESPECT those (fill gaps only).
        print(f"[{tag}] {url.split('/job/')[-1][:50]}", flush=True)
        creds = creds_for(tenant)  # REUSE the tenant's account (sign in), don't create a new one each run
        res = None
        for _try in range(2):  # sign-in; if the tracked account is REJECTED, rotate to a FRESH create once
            try:
                res = await _attempt(url, creds)
            except TimeoutError:
                print(f"[{tag}] TIMEOUT", flush=True)
                return
            except Exception as exc:
                print(f"[{tag}] ERROR {type(exc).__name__}: {exc}", flush=True)
                res = None
                break
            # A tracked account Workday no longer accepts must NOT be retried as-is — mint a fresh account
            # (existing=False -> authenticate() takes the CREATE path) and try this same url once more.
            if _try == 0 and res.get("status") == "AUTH_FAILED" and "SIGN_IN" in (res.get("reason") or ""):
                print(f"[{tag}] tracked account rejected -> minting FRESH account (rotate)", flush=True)
                creds = rotate_creds(tenant)
                continue
            break
        if res is None:
            continue
        st = res.get("status", "?")
        steps = res.get("steps", [])
        print(f"[{tag}] status={st} steps={len(steps)} cost=${res.get('cost', 0):.4f}", flush=True)
        if st in ("FILLED_TO_REVIEW", "FILLED") or any("xperience" in (s.get("name") or "") for s in steps):
            print(f"[{tag}] >>> REACHED+FILLED (screenshots: {tag}_step*.png / {tag}_review.png)", flush=True)
            return
    print(f"[{tag}] exhausted job attempts", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush()
    os._exit(0)
