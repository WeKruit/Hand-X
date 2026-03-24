"""Apply to a job with DomHand — the Hand-X entry point.

Usage:
    # Simplest — just a URL (opens visible browser, uses sample data)
    python examples/apply_to_job.py --job-url "https://job-boards.greenhouse.io/starburst/jobs/5123053008"

    # With your own data + resume PDF
    python examples/apply_to_job.py --job-url "https://..." --test-data my_info.json --resume my_resume.pdf

    # Workday job
    python examples/apply_to_job.py --job-url "https://company.wd5.myworkdayjobs.com/External/job/NYC/SWE_12345"

    # With login credentials
    python examples/apply_to_job.py --job-url "https://..." --email you@email.com --password yourpass

    # Quick test (fewer steps, lower budget)
    python examples/apply_to_job.py --job-url "https://..." --max-steps 15

    # Choose model
    python examples/apply_to_job.py --job-url "https://..." --model claude-sonnet-4-0

Requires:
    GOOGLE_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, or GH_LLM_PROXY_URL.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ── Project root on path ──────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import atexit
import stat
import tempfile

from browser_use import Agent, Browser, BrowserProfile, Tools
from ghosthands.agent.hooks import install_same_tab_guard
from ghosthands.agent.prompts import (
    _format_profile_summary,
    build_system_prompt,
    build_task_prompt,
)
from ghosthands.bridge.profile_adapter import (
    camel_to_snake_profile,
    normalize_profile_defaults,
)
from ghosthands.config.settings import settings

# ── Defaults ──────────────────────────────────────────────────────────
EXAMPLES_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = EXAMPLES_DIR / "apply_to_job_sample_data.json"
DEFAULT_RESUME = EXAMPLES_DIR / "resume.pdf"
DEFAULT_JOB_URL = "https://job-boards.greenhouse.io/starburst/jobs/5123053008"

DEFAULT_MODEL = "gemini-3-flash-preview"


def _get_llm(model: str | None = None):
    """Get LLM — uses VALET proxy if GH_LLM_PROXY_URL is set."""
    try:
        from ghosthands.llm.client import get_chat_model

        return get_chat_model(model=model or DEFAULT_MODEL)
    except ImportError:
        # Fallback for running without ghosthands installed
        model = model or DEFAULT_MODEL
        if model.startswith("gpt-") or model.startswith("o"):
            from browser_use import ChatOpenAI

            return ChatOpenAI(model=model)
        if model.startswith("claude-"):
            from browser_use import ChatAnthropic

            return ChatAnthropic(model=model)
        from browser_use import ChatGoogle

        return ChatGoogle(model=model)


async def apply_to_job(
    info: dict,
    resume_path: str,
    job_url: str,
    model: str | None = None,
    email: str | None = None,
    password: str | None = None,
    headless: bool = False,
    max_steps: int = 50,
    max_budget: float = 1.00,
):
    llm = _get_llm(model=model)

    # ── Normalize profile (same flow as CLI / Desktop bridge) ─────
    info = camel_to_snake_profile(info)
    info = normalize_profile_defaults(info)

    # ── Write profile to temp file (matches CLI's _apply_runtime_env) ──
    profile_fd, profile_path = tempfile.mkstemp(prefix="gh_profile_", suffix=".json")
    try:
        os.fchmod(profile_fd, stat.S_IRUSR | stat.S_IWUSR)
        os.write(profile_fd, json.dumps(info, indent=2).encode())
        os.close(profile_fd)
        os.environ["GH_USER_PROFILE_PATH"] = profile_path
        atexit.register(lambda: os.unlink(profile_path) if os.path.exists(profile_path) else None)
    except Exception:
        os.close(profile_fd)
        os.unlink(profile_path)
        raise

    os.environ["GH_USER_PROFILE_TEXT"] = _format_profile_summary(info)
    os.environ["GH_USER_PROFILE_JSON"] = json.dumps(info)
    if resume_path:
        os.environ["GH_RESUME_PATH"] = str(resume_path)

    # ── Register DomHand actions ──────────────────────────────────
    tools = Tools()
    try:
        from ghosthands.actions import register_domhand_actions

        register_domhand_actions(tools)
        print("DomHand actions registered (DOM-first form filling active)")
    except Exception as e:
        print(f"DomHand not available ({e}), using vanilla browser-use")

    # ── Detect platform for guardrails ────────────────────────────
    platform = "generic"
    try:
        from ghosthands.platforms import detect_platform

        platform = detect_platform(job_url)
    except ImportError:
        pass
    print(f"Platform: {platform}")

    # ── Build system prompt with profile + guardrails ─────────────
    system_ext = build_system_prompt(info, platform)

    # ── Credentials as sensitive_data ─────────────────────────────
    sensitive_data = None
    if email and password:
        sensitive_data = {"email": email, "password": password}

    # ── Browser config ────────────────────────────────────────────
    browser = Browser(
        browser_profile=BrowserProfile(
            headless=headless,
            keep_alive=True,
            wait_between_actions=settings.wait_between_actions,
        ),
    )

    # ── Task prompt (same as CLI's build_task_prompt) ─────────────
    task = build_task_prompt(
        job_url,
        resume_path,
        sensitive_data,
        platform=platform,
    )

    async def _on_step_start(agent_instance):
        await install_same_tab_guard(agent_instance)
        try:
            pass
        except Exception:
            pass

    # ── Create agent ──────────────────────────────────────────────
    available_files = [resume_path] if resume_path else []
    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        tools=tools,
        extend_system_message=system_ext or None,
        sensitive_data=sensitive_data,
        available_file_paths=available_files or None,
        use_vision="auto",
        max_actions_per_step=5,
        calculate_cost=True,
    )

    # ── Run ───────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"  URL:       {job_url}")
    print(f"  Platform:  {platform}")
    print(f"  Model:     {llm.model if hasattr(llm, 'model') else '?'}")
    print(f"  Resume:    {resume_path}")
    print(f"  Headless:  {headless}")
    print(f"  Max steps: {max_steps}")
    proxy_url = os.environ.get("GH_LLM_PROXY_URL", "")
    if proxy_url:
        print(f"  LLM Proxy: {proxy_url}")
    else:
        print("  LLM:       Direct API")
    print("=" * 60)
    print()

    history = await agent.run(max_steps=max_steps, on_step_start=_on_step_start)

    # ── Results ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  RESULT")
    print("=" * 60)
    print(f"  Done:    {history.is_done()}")
    print(f"  Steps:   {len(history.history)}")
    if history.usage:
        try:
            print(f"  Cost:    ${history.usage.total_cost:.4f}")
            in_tok = getattr(history.usage, "total_prompt_tokens", None) or getattr(
                history.usage, "total_input_tokens", 0
            )
            out_tok = getattr(history.usage, "total_completion_tokens", None) or getattr(
                history.usage, "total_output_tokens", 0
            )
            print(f"  Tokens:  {in_tok} in / {out_tok} out")
        except Exception:
            print("  (token stats unavailable)")
    result = history.final_result()
    if result:
        print(f"  Output:  {result[:500]}")
    print("=" * 60)
    print()
    print("  Browser is still open — review the application before submitting.")
    print("  Press Ctrl+C to close when done.")
    print()

    # Keep process alive so browser stays open for review
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nClosing browser...")
        await browser.kill()

    return result


async def main():
    parser = argparse.ArgumentParser(description="Apply to a job with Hand-X + DomHand")
    parser.add_argument("--job-url", default=DEFAULT_JOB_URL, help="Job posting URL")
    parser.add_argument("--test-data", default=str(DEFAULT_DATA), help="Applicant info JSON")
    parser.add_argument("--resume", default=str(DEFAULT_RESUME), help="Resume PDF path")
    parser.add_argument("--model", default=None, help="LLM model (claude-sonnet-4-0, o3, etc.)")
    parser.add_argument("--email", default=None, help="Login email")
    parser.add_argument("--password", default=None, help="Login password")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--max-steps", type=int, default=50, help="Max steps (default: 50)")
    parser.add_argument("--max-budget", type=float, default=1.00, help="Max LLM budget USD")
    parser.add_argument("--proxy-url", default=None, help="VALET LLM proxy URL (routes all LLM calls through VALET)")
    parser.add_argument("--runtime-grant", default=None, help="VALET runtime grant token for managed inference")
    args = parser.parse_args()

    # Set proxy env vars if provided
    if args.proxy_url:
        os.environ["GH_LLM_PROXY_URL"] = args.proxy_url
    if args.runtime_grant:
        os.environ["GH_LLM_RUNTIME_GRANT"] = args.runtime_grant

    # Validate files
    if not Path(args.test_data).exists():
        print(f"ERROR: Test data not found: {args.test_data}")
        print("  Create one or use: --test-data path/to/data.json")
        sys.exit(1)
    if not Path(args.resume).exists():
        print(f"ERROR: Resume not found: {args.resume}")
        print("  Add a resume PDF or use: --resume path/to/resume.pdf")
        sys.exit(1)

    with open(args.test_data) as f:
        info = json.load(f)

    # If using ghosthands resume_loader format, normalize it
    try:
        from ghosthands.integrations.resume_loader import load_resume_from_file

        info = load_resume_from_file(args.test_data)
    except Exception:
        pass  # Use raw JSON as-is

    result = await apply_to_job(
        info=info,
        resume_path=str(Path(args.resume).resolve()),
        job_url=args.job_url,
        model=args.model,
        email=args.email,
        password=args.password,
        headless=args.headless,
        max_steps=args.max_steps,
        max_budget=args.max_budget,
    )
    print("\nResult:", result)


if __name__ == "__main__":
    asyncio.run(main())
