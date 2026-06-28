"""Cloud variant — browser-use CLOUD 'Skills' as the persistent, auto-healing script cache.

Why this exists: the LOCAL `replay` (jobapply.py) re-runs a saved trajectory with NO
LLM ($0) but has no auto-heal — it breaks the moment a recorded element moves
(observed: react-select option `react-select-school--0-option-0` is ephemeral, so a
deterministic re-click fails mid-form). The cloud **Skills** API is the hosted
equivalent that DOES auto-heal: record once -> a reusable skill (the cached script);
execute per-applicant with NAMED parameters -> $0 LLM on a cache hit, and it
regenerates the script (~$0.05-1.00) when the page changes.

Verified against the installed SDK (browser-use-sdk 3.8.4):
  * the same BROWSER_USE_API_KEY authorizes the cloud API (tasks.list returned OK)
  * primitive is c.skills.create(goal, agent_prompt) + c.skills.execute(id, parameters=...)
    (the docs' `workspaces` object does NOT exist in this SDK version)
  * ExecuteSkillResponse = success/result/error/stderr/latency_ms — NO cost field, so
    the $0-cache proof is via latency (cache hit ~secs vs agent minutes) + billing delta
  * resume upload = per-SESSION presigned PUT: c.files.session_url(session_id,
    file_name, content_type, size_bytes) -> PUT the bytes -> run the skill in that
    session via skills.execute(..., session_id=...)

Install (separate env from the local lib to avoid dep clashes):
    pip install browser-use-sdk httpx

Usage:
  BROWSER_USE_API_KEY=... python jobapply_cloud.py smoke      # prove $0-on-cache-hit
  BROWSER_USE_API_KEY=... python jobapply_cloud.py apply --job-url <URL> \
        --profile <p.json> --resume <pdf>                     # real cloud apply (fill-only)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from jobapply import build_instructions  # reuse the SAME standing-instruction block

HERE = Path(__file__).resolve().parent


def _client() -> Any:
    from browser_use_sdk import BrowserUse  # pip install browser-use-sdk

    key = os.environ.get("BROWSER_USE_API_KEY")
    if not key:
        raise SystemExit("Set BROWSER_USE_API_KEY (the same key authorizes cloud).")
    return BrowserUse(api_key=key)


def _id(obj: Any) -> str | None:
    for a in ("id", "skill_id", "session_id"):
        v = getattr(obj, a, None)
        if v:
            return str(v)
    return None


def _credits(c: Any) -> float | None:
    """Best-effort credit balance, to measure $ spent across a run."""
    try:
        acct = c.billing.account()
    except Exception:
        return None
    for f in ("credits_remaining", "credits", "balance", "remaining", "credit_balance"):
        v = getattr(acct, f, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


_PENDING = {"pending", "running", "generating", "in_progress", "queued", "processing",
            "recording", "training", "building", "analyzing", "started", "creating"}
_READY = {"finished", "completed", "succeeded", "success", "ready", "published", "enabled", "done"}


def _wait_ready(c: Any, sid: str, *, timeout_s: int = 900) -> Any:
    """A new skill GENERATES asynchronously (the cloud agent runs the goal once to
    build the deterministic script — this is the one-time $ cost). Poll until that
    finishes, then ensure the skill is enabled so it can be executed."""
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        sk = c.skills.get(sid)
        status = str(getattr(getattr(sk, "status", None), "value", getattr(sk, "status", ""))).lower()
        if status not in _PENDING:
            break
        if time.monotonic() > deadline:
            raise SystemExit(f"[cloud] skill {sid} still '{status}' after {timeout_s}s")
        time.sleep(4)
    print(f"[cloud] generation status={status} enabled={getattr(sk, 'is_enabled', None)} "
          f"params={[getattr(p, 'name', p) for p in getattr(sk, 'parameters', []) or []]}")
    if status in {"failed", "error"}:
        raise SystemExit(f"[cloud] skill generation FAILED for {sid} (goal too vague / unrunnable).")
    if not getattr(sk, "is_enabled", False):
        c.skills.update(sid, is_enabled=True)
        print("[cloud] enabled skill")
    return sk


def smoke(_args: argparse.Namespace) -> None:
    """End-to-end proof of the cache primitive. Generation (one-time, $) builds the
    script from a CONCRETE runnable task; then every execute is a $0 cache hit (fast)."""
    c = _client()
    print("[cloud] auth ok; existing skills:", c.skills.list().total_items)
    before_build = _credits(c)
    skill = c.skills.create(
        title="smoke-page-title",
        # concrete + runnable so generation can actually record a script; placeholder
        # only on a VALUE (the count), never the whole URL.
        goal="Open https://news.ycombinator.com and return the titles of the top stories as JSON.",
        agent_prompt='Open https://news.ycombinator.com. Return the top @{{count}} story titles as {"titles": [...]}.',
    )
    sid = _id(skill)
    print(f"[cloud] created skill {sid}; waiting for generation…")
    _wait_ready(c, sid)
    after_build = _credits(c)
    build_cost = None if (before_build is None or after_build is None) else round(before_build - after_build, 4)
    print(f"[cloud] one-time generation credits_spent={build_cost}")
    for i in (1, 2):
        before = _credits(c)
        out = c.skills.execute(sid, parameters={"count": "5"})
        after = _credits(c)
        spent = None if (before is None or after is None) else round(before - after, 4)
        print(
            f"[cloud] exec {i}: success={getattr(out, 'success', None)} "
            f"latency={getattr(out, 'latency_ms', None)}ms credits_spent={spent} "
            f"result={str(getattr(out, 'result', None))[:80]!r}"
        )
    print("[cloud] each exec = deterministic cached script, $0 LLM (cost was the one-time generation).")


def _upload_resume(c: Any, resume: Path, session_id: str) -> str:
    """Upload the resume PDF to the cloud browser session via presigned PUT, so the
    cloud agent can pick it in a file-upload widget. Returns the cloud-visible name."""
    import httpx

    pres = c.files.session_url(
        session_id,
        file_name=resume.name,
        content_type="application/pdf",
        size_bytes=resume.stat().st_size,
    )
    put_url = getattr(pres, "url", None) or getattr(pres, "presigned_url", None) or getattr(pres, "upload_url", None)
    if not put_url:
        raise SystemExit(f"no presigned PUT url on response: {pres!r}")
    r = httpx.put(put_url, content=resume.read_bytes(), headers={"Content-Type": "application/pdf"}, timeout=60)
    r.raise_for_status()
    return getattr(pres, "name", None) or resume.name


def apply(args: argparse.Namespace) -> None:
    """Record-once-as-skill, then execute for an applicant. Re-running the same skill
    for the next applicant hits the cached script ($0 LLM, auto-heals on page change).

    Remaining gap: email verification code — the cloud agent cannot read your Gmail;
    pass the code via skills.execute(parameters=...) once fetched (user: 'email later')."""
    c = _client()
    profile = json.loads(Path(args.profile).read_text())

    # One skill per job posting: the URL is baked in (concrete, so generation can run);
    # the per-applicant fields are the @{{}} parameters reused across users.
    agent_prompt = (
        build_instructions(submit=args.submit)
        + f"\n\nOpen {args.job_url} and fill EVERY field from the named parameters provided "
        "at execution time (first_name @{{first_name}}, last_name @{{last_name}}, email "
        "@{{email}}, phone @{{phone}}, plus location, links, experience, education, skills, "
        "cover_letter, eeo_optional). The resume PDF is uploaded to this session as "
        "@{{resume_name}} — use it for file-upload fields."
    )
    skill = c.skills.create(
        title="greenhouse-apply",
        goal=f"Fill (and optionally submit) the Greenhouse application at {args.job_url} from named applicant parameters.",
        agent_prompt=agent_prompt,
    )
    sid = _id(skill)
    print(f"[cloud] created apply skill {sid}; waiting for generation…")
    _wait_ready(c, sid)

    sess = c.sessions.create(start_url=args.job_url, keep_alive=True)
    session_id = _id(sess)
    print(f"[cloud] session {session_id}")

    params: dict[str, Any] = dict(profile)
    if args.resume:
        params["resume_name"] = _upload_resume(c, Path(args.resume), session_id)
        print(f"[cloud] uploaded resume -> {params['resume_name']}")

    out = c.skills.execute(sid, parameters=params, session_id=session_id)
    print(f"[cloud] execute success={getattr(out, 'success', None)} latency={getattr(out, 'latency_ms', None)}ms")
    print(f"[cloud] result={str(getattr(out, 'result', None))[:300]}")
    print("[cloud] re-run the SAME skill for the next applicant -> $0 on cache hit (auto-heals on page change).")


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(HERE / ".env")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Cloud (Skills) variant of the job-application submitter")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("smoke", help="Prove $0-on-cache-hit with a trivial skill")

    ap = sub.add_parser("apply", help="Create an apply skill and execute it with a profile")
    ap.add_argument("--job-url", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--resume", default=None)
    ap.add_argument("--submit", action="store_true", help="Actually submit (IRREVERSIBLE).")

    args = p.parse_args()
    {"smoke": smoke, "apply": apply}[args.cmd](args)


if __name__ == "__main__":
    main()
