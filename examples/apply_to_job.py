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

from browser_use import Agent, Browser, BrowserProfile, ChatAnthropic, Tools
from browser_use.tools.views import UploadFileAction

# ── Defaults ──────────────────────────────────────────────────────────
EXAMPLES_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = EXAMPLES_DIR / "apply_to_job_sample_data.json"
DEFAULT_RESUME = EXAMPLES_DIR / "resume.pdf"
DEFAULT_JOB_URL = "https://job-boards.greenhouse.io/starburst/jobs/5123053008"


def _get_llm(model: str | None = None):
	if model:
		if model.startswith("gpt-") or model.startswith("o"):
			from browser_use import ChatOpenAI
			return ChatOpenAI(model=model)
		return ChatAnthropic(model=model)
	if os.environ.get("OPENAI_API_KEY"):
		from browser_use import ChatOpenAI
		return ChatOpenAI(model="o3")
	if os.environ.get("ANTHROPIC_API_KEY"):
		return ChatAnthropic(model="claude-sonnet-4-0")
	raise RuntimeError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env")


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
		),
	)

	# ── Task prompt ───────────────────────────────────────────────
	task = f"""Go to {job_url} and fill out the job application form completely.

Applicant information:
{json.dumps(info, indent=2)}

Instructions:
- Fill every field from top to bottom. Do not skip fields.
- For the resume, upload the file at: {resume_path}
- Use domhand_fill when available — it fills form fields via DOM manipulation (faster and cheaper).
- For dropdowns, use domhand_select if available.
- For file uploads, use upload_file_to_element with the resume path.
- If anything pops up blocking the form, close it and continue.
- {'Use the provided credentials to log in if needed.' if sensitive_data else 'If a login wall appears, report it as a blocker.'}
- Do NOT click the final Submit button. Stop at the review page and use the done action.
- At the end, output a summary of all fields filled and any issues encountered.
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
		print(f"  Tokens:  {history.usage.total_input_tokens} in / {history.usage.total_output_tokens} out")
	result = history.final_result()
	if result:
		print(f"  Output:  {result[:500]}")
	print("=" * 60)

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
	args = parser.parse_args()

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
