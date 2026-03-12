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
    ANTHROPIC_API_KEY or OPENAI_API_KEY in environment or .env file.
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

from browser_use import Agent, Browser, BrowserProfile, ChatGoogle, Tools
from browser_use.tools.views import UploadFileAction

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

	# ── Set profile text for DomHand (domhand_fill reads from env) ─
	os.environ["GH_USER_PROFILE_TEXT"] = json.dumps(info, indent=2)
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
	system_ext = ""
	try:
		from ghosthands.agent.prompts import build_system_prompt
		system_ext = build_system_prompt(info, platform)
	except ImportError:
		pass

	# ── Credentials as sensitive_data ─────────────────────────────
	sensitive_data = None
	if email and password:
		sensitive_data = {"email": email, "password": password}

	# ── Browser config ────────────────────────────────────────────
	browser = Browser(
		browser_profile=BrowserProfile(
			headless=headless,
			keep_alive=True,  # Keep browser open for user review
		),
	)

	# ── Task prompt ───────────────────────────────────────────────
	task = f"""Go to {job_url} and fill out the job application form completely.

CRITICAL — Action Order:
1. After navigating to the page, your FIRST action MUST be domhand_fill. It fills ALL visible form fields in one call via DOM manipulation. Do NOT use click or input actions before trying domhand_fill.
2. After domhand_fill completes, review its output to see which fields were filled and which failed.
3. For failed dropdowns/selects, use domhand_select.
4. For file uploads (resume), use domhand_upload or upload_file action with path: {resume_path}
5. Only use generic browser-use actions (click, input_text) as a LAST RESORT for fields DomHand could not handle.
6. After all fields on the current page are filled, click Next/Continue/Save to advance.
7. On each new page, call domhand_fill AGAIN as the first action.

Other rules:
- {'Use the provided credentials to log in or create an account if needed. For Workday, fill email + password + confirm password on the Create Account page.' if sensitive_data else 'If a login wall appears, report it as a blocker.'}
- Do NOT click the final Submit button. Stop at the review page and use the done action.
- If anything pops up blocking the form, close it and continue.
"""

	# ── Create agent ──────────────────────────────────────────────
	agent = Agent(
		task=task,
		llm=llm,
		browser=browser,
		tools=tools,
		extend_system_message=system_ext if system_ext else None,
		sensitive_data=sensitive_data,
		available_file_paths=[resume_path],
		use_vision=True,
		max_actions_per_step=5,
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
		print(f"  LLM:       Direct API")
	print("=" * 60)
	print()

	history = await agent.run(max_steps=max_steps)

	# ── Results ───────────────────────────────────────────────────
	print()
	print("=" * 60)
	print("  RESULT")
	print("=" * 60)
	print(f"  Done:    {history.is_done()}")
	print(f"  Steps:   {len(history.history)}")
	if history.usage:
		print(f"  Cost:    ${history.usage.total_cost:.4f}")
		print(f"  Tokens:  {history.usage.total_prompt_tokens} in / {history.usage.total_completion_tokens} out")
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
		print(f"  Create one or use: --test-data path/to/data.json")
		sys.exit(1)
	if not Path(args.resume).exists():
		print(f"ERROR: Resume not found: {args.resume}")
		print(f"  Add a resume PDF or use: --resume path/to/resume.pdf")
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
