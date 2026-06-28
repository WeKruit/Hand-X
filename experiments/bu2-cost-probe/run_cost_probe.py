"""Standalone bu-2-0 cost probe — vanilla browser-use, no DomHand / no GhostHands.

Runs the *upstream* browser-use Agent against a single job posting, fills the
application form, and prints the token usage + cost for the run.

This file imports ONLY ``browser_use``. It has no dependency on the ghosthands
package, DomHand actions, custom system prompts, platform guardrails, or the
VALET proxy. Point it at a fresh, latest install of browser-use (see README.md)
so the number you get is "vanilla browser-use + bu model", nothing else.

Quickstart
----------
    uv venv --python 3.12 .venv-bu2 && source .venv-bu2/bin/activate
    uv pip install -U browser-use
    playwright install chromium
    export BROWSER_USE_API_KEY=...     # https://cloud.browser-use.com/new-api-key

    # Fill only — stops before the final Submit button (safe default):
    python run_cost_probe.py --job-url "https://job-boards.greenhouse.io/<org>/jobs/<id>"

    # Compare against our Gemini default (needs GOOGLE_API_KEY instead):
    python run_cost_probe.py --job-url "..." --model gemini-3-flash-preview

    # Actually submit (IRREVERSIBLE — sends a real application to the employer):
    python run_cost_probe.py --job-url "..." --submit
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from browser_use import Agent, Browser, BrowserProfile

HERE = Path(__file__).resolve().parent
DEFAULT_PROFILE = HERE / "sample_profile.json"

# Fill-only is the default: we measure the cost of filling a real form without
# committing a real application to someone's ATS. Flip with --submit.
_FILL_ONLY = (
    "Fill in EVERY field of the job application form accurately from the applicant "
    "profile below, including uploading the resume if a file-upload control is present. "
    "Do NOT click the final Submit / Apply / Send button. Once every visible field is "
    "filled, stop and report which fields you filled and anything you could not."
)
_SUBMIT = (
    "Fill in EVERY field of the job application form accurately from the applicant "
    "profile below, including uploading the resume if a file-upload control is present, "
    "then click the final Submit / Apply button to submit the application."
)


def build_task(job_url: str, profile: dict, resume_path: str | None, submit: bool) -> str:
    parts = [
        f"Open {job_url} and complete the job application.",
        _SUBMIT if submit else _FILL_ONLY,
        "",
        "Applicant profile (JSON):",
        json.dumps(profile, indent=2),
    ]
    if resume_path:
        parts.append(f"\nResume file is available locally at: {resume_path}")
    return "\n".join(parts)


def _make_llm(model: str):
    """Pick the chat model from the bare model id — no proxy, no ghosthands routing."""
    if model.startswith("bu-") or model in {"smart"}:
        from browser_use import ChatBrowserUse  # needs BROWSER_USE_API_KEY

        return ChatBrowserUse(model=model)
    if model.startswith("gemini") or model.startswith("models/"):
        from browser_use import ChatGoogle  # needs GOOGLE_API_KEY

        return ChatGoogle(model=model)
    if model.startswith("claude-"):
        from browser_use import ChatAnthropic  # needs ANTHROPIC_API_KEY

        return ChatAnthropic(model=model)
    if model.startswith("gpt-") or model.startswith("o"):
        from browser_use import ChatOpenAI  # needs OPENAI_API_KEY

        return ChatOpenAI(model=model)
    raise SystemExit(f"Unrecognised model id: {model!r}")


async def run(args: argparse.Namespace) -> None:
    profile = json.loads(Path(args.profile).read_text())
    resume = str(Path(args.resume).resolve()) if args.resume else None

    llm = _make_llm(args.model)
    browser = Browser(
        browser_profile=BrowserProfile(headless=args.headless, keep_alive=True),
    )

    agent = Agent(
        task=build_task(args.job_url, profile, resume, args.submit),
        llm=llm,
        browser=browser,
        available_file_paths=[resume] if resume else None,
        use_vision="auto",
        max_actions_per_step=5,
        calculate_cost=True,  # populates history.usage with per-model cost
    )

    print("=" * 64)
    print(f"  model:     {args.model}")
    print(f"  job url:   {args.job_url}")
    print(f"  mode:      {'SUBMIT (live!)' if args.submit else 'fill-only (no submit)'}")
    print(f"  headless:  {args.headless}")
    print(f"  max steps: {args.max_steps}")
    print("=" * 64)

    history = await agent.run(max_steps=args.max_steps)

    u = history.usage
    print("\n" + "=" * 64)
    print("  RESULT")
    print("=" * 64)
    print(f"  done:   {history.is_done()}")
    print(f"  steps:  {len(history.history)}")
    if u is not None:
        print(f"  TOTAL COST:        ${u.total_cost:.4f}")
        print(f"  prompt tokens:     {u.total_prompt_tokens:,}  (${u.total_prompt_cost:.4f})")
        print(f"  cached tokens:     {u.total_prompt_cached_tokens:,}  (${u.total_prompt_cached_cost:.4f})")
        print(f"  completion tokens: {u.total_completion_tokens:,}  (${u.total_completion_cost:.4f})")
        print(f"  total tokens:      {u.total_tokens:,}")
    else:
        print("  (no usage recorded — calculate_cost produced nothing)")
    final = history.final_result()
    if final:
        print(f"\n  output: {final[:500]}")
    print("=" * 64)

    if not args.headless:
        print("\n  Browser left open for review. Ctrl+C to close.")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
    await browser.kill()


def main() -> None:
    p = argparse.ArgumentParser(description="Vanilla browser-use job-application cost probe")
    p.add_argument("--job-url", required=True, help="Greenhouse (or any ATS) job posting URL")
    p.add_argument("--model", default="bu-2-0", help="Model id (default: bu-2-0)")
    p.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Applicant profile JSON")
    p.add_argument("--resume", default=None, help="Path to resume PDF (optional)")
    p.add_argument("--max-steps", type=int, default=40, help="Max agent steps (default: 40)")
    p.add_argument("--headless", action="store_true", help="Run headless (CI)")
    p.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit the application (IRREVERSIBLE). Default: fill only.",
    )
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
