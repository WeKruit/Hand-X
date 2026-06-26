"""Cloud variant — browser-use CLOUD 'Skills' as the persistent script cache.

Why this exists: the LOCAL `replay` (jobapply.py) re-runs a saved trajectory with
NO LLM ($0) but it has no auto-heal — it breaks the moment a recorded element moves
(observed: react-select option `react-select-school--0-option-0` is ephemeral, so a
deterministic re-click fails mid-form). The cloud **Skills** API is the hosted
equivalent that DOES auto-heal: record once -> a reusable skill (the cached script);
execute per-applicant with named parameters -> $0 LLM on a cache hit, and it
regenerates the script (~$0.05-1.00) when the page changes.

Verified against the installed SDK (browser-use-sdk 3.8.4):
  * the same BROWSER_USE_API_KEY authorizes the cloud API (tasks.list returned OK)
  * the real primitive is c.skills.create / c.skills.execute (the docs' `workspaces`
    object does NOT exist in this SDK version)
  * skills.execute takes a NAMED `parameters` dict (nicer than the docs' positional
    @{{}} placeholders) — maps cleanly onto our profile JSON
  * resume upload is a presigned-PUT flow (UploadFilePresignedUrlResponse: "send a
    PUT request to this URL with the file content"); see _upload_resume TODO

Install (separate from the local lib):  pip install browser-use-sdk

Usage:
  python jobapply_cloud.py smoke         # prove $0-on-cache-hit with a trivial skill
  python jobapply_cloud.py apply --job-url <URL> --profile <p.json> --resume <pdf>
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

# reuse the SAME standing-instruction block as the local engine
from jobapply import build_instructions

HERE = Path(__file__).resolve().parent


def _client() -> Any:
    from browser_use_sdk import BrowserUse  # pip install browser-use-sdk

    key = os.environ.get("BROWSER_USE_API_KEY")
    if not key:
        raise SystemExit("Set BROWSER_USE_API_KEY (the same key works for cloud).")
    return BrowserUse(api_key=key)


def _cost(obj: Any) -> str:
    for attr in ("llm_cost_usd", "total_cost", "cost"):
        v = getattr(obj, attr, None)
        if v is not None:
            return f"${float(v):.4f}"
    return "n/a"


def smoke(_args: argparse.Namespace) -> None:
    """Minimal end-to-end proof of the cache primitive: create a skill, execute it
    twice. Run 1 builds the script (small $); run 2 is a cache hit ($0 LLM)."""
    c = _client()
    print("[cloud] auth ok; existing skills:", c.skills.list().total_items)
    skill = c.skills.create(
        title="smoke-page-title",
        goal="Open the given url and return its page <title> text as JSON.",
        agent_prompt="Open @{{url}}. Read the document title. Return {\"title\": <text>}.",
    )
    sid = getattr(skill, "id", None) or getattr(skill, "skill_id", None)
    print(f"[cloud] created skill {sid}")
    for i in (1, 2):
        out = c.skills.execute(sid, parameters={"url": "https://example.com"})
        print(f"[cloud] run {i}: cost={_cost(out)}  output={str(getattr(out, 'output', out))[:120]}")
    print("[cloud] expectation: run 1 > $0 (agent builds script), run 2 == $0 (cache hit).")


def _upload_resume(c: Any, resume: Path) -> str:
    """Resume upload for the cloud browser via presigned PUT.

    TODO(verify): the v3 surface for this in browser-use-sdk 3.8.4 is a presigned
    upload (models.py: 'Presigned PUT URL. Upload the file by sending a PUT request
    to this URL with the file content and matching Content-Type header.'). Wire:
      1) request the presigned PUT url for `resume.name` (v2 files resource /
         UploadFilePresignedUrlResponse),
      2) httpx.put(url, content=resume.read_bytes(), headers={'Content-Type': 'application/pdf'}),
      3) reference the uploaded name in the task so the cloud agent can pick it in
         the file-upload widget.
    Returns the cloud-visible reference. Stubbed until the exact resource is pinned.
    """
    raise NotImplementedError(
        "Cloud resume upload (presigned PUT) not wired yet — see _upload_resume docstring."
    )


def apply(args: argparse.Namespace) -> None:
    """Scaffold: record-once-as-skill, then execute per applicant with named params.

    Gaps to close before this submits a real application:
      * resume upload  -> _upload_resume (presigned PUT)
      * email verification code -> cloud agent cannot read your Gmail; pass the code
        via skills.execute(parameters=...) once fetched, or via the `secrets` channel.
        (User: 'come to email later'.)
    """
    c = _client()
    profile = json.loads(Path(args.profile).read_text())

    # The skill is the cached script. Its agent_prompt = our shared instruction block
    # plus a directive to read every field from the named parameters.
    agent_prompt = (
        build_instructions(submit=args.submit)
        + "\n\nFill EVERY field from the named parameters provided at execution time "
        "(first_name, last_name, email, phone, location, links, experience, education, "
        "skills, cover_letter, eeo_optional). Open @{{job_url}} first."
    )
    skill = c.skills.create(
        title="greenhouse-apply",
        goal="Fill (and optionally submit) a Greenhouse job application from named applicant parameters.",
        agent_prompt=agent_prompt,
    )
    sid = getattr(skill, "id", None) or getattr(skill, "skill_id", None)
    print(f"[cloud] created apply skill {sid}")

    params: dict[str, Any] = {"job_url": args.job_url, **profile}
    if args.resume:
        params["resume_ref"] = _upload_resume(c, Path(args.resume))  # raises until wired

    out = c.skills.execute(sid, parameters=params)
    print(f"[cloud] execute cost={_cost(out)}")
    print(f"[cloud] output={getattr(out, 'output', out)!r}")
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
