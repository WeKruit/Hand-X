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

from browser_use import Agent, Browser, BrowserProfile, ChatGoogle, Tools
from browser_use.tools.views import UploadFileAction
from ghosthands.agent.hooks import install_same_tab_guard
from ghosthands.agent.prompts import _format_profile_summary
from ghosthands.config.settings import settings

# ── Defaults ──────────────────────────────────────────────────────────
EXAMPLES_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = EXAMPLES_DIR / "apply_to_job_sample_data.json"
DEFAULT_RESUME = EXAMPLES_DIR / "resume.pdf"
DEFAULT_JOB_URL = "https://job-boards.greenhouse.io/starburst/jobs/5123053008"

DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"


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
			wait_between_actions=settings.wait_between_actions,
		),
	)

	# ── Task prompt ───────────────────────────────────────────────
	workday_start_flow_rules = ""
	if platform == "workday":
		workday_start_flow_rules = (
			"- If a start dialog offers a SAME-SITE option such as 'Autofill with Resume'\n"
			"  or 'Apply with Resume', prefer that path over manual entry.\n"
			"- Do NOT choose external apply/import options such as LinkedIn, Indeed,\n"
			"  Google, or other third-party account flows.\n"
			"- After uploading a resume on Workday, WAIT for the filename or a\n"
			"  success message to appear and for the Continue button to become\n"
			"  enabled before clicking it.\n"
			"- Do NOT upload a resume and click Continue in the same action batch.\n"
		)

	task = f"""Go to {job_url} and fill out the job application form completely.

FORM PAGE SEQUENCE (repeat on EVERY form page):
1. domhand_fill — fills all visible fields in one call. ALWAYS first.
2. Handle domhand_fill's unresolved fields with domhand_select or click.
   Do this for REQUIRED fields.
   For OPTIONAL fields, only do it when the applicant profile clearly maps
   to that field with high confidence (address, LinkedIn, website,
   referral source, etc.). If the optional match is ambiguous, leave it blank.
3. Upload resume: domhand_upload or upload_file with path: {resume_path}
4. For repeater sections (Work Experience, Education):
   a. Call domhand_expand(section="Work Experience") to click Add
   b. Call domhand_fill with heading_boundary matching the new entry heading
      and entry_data containing ONLY that one profile entry
   c. Repeat for each entry in the applicant profile
5. AFTER all fields are filled: click Next / Continue / Save & Continue.
   *** YOU MUST CLICK NEXT. Do NOT call done() until you reach a
   read-only review/confirmation page with no editable fields. ***
6. On the new page, start over from step 1.

AUTH PAGE SEQUENCE (Create Account / Sign In):
Do NOT call domhand_fill on auth pages — it uses the wrong email.
(1) input credentials, (2) domhand_check_agreement for 'I agree' checkbox,
(3) VERIFY checkbox is checked, (4) domhand_click_button to submit.

TRANSITION RULE:
If the page looks blank or half-loaded after clicking a start/continue button,
WAIT 5-10 seconds before going back, reopening the dialog, or retrying the click.
Never use navigate() to go back to the original job URL after entering the
application flow. Waiting is the default recovery, not restarting.

DROPDOWN RULE: If domhand_select returns [FAIL-OVER], STOP retrying it.
Click the dropdown open yourself, find the option visually, click it.
If a dropdown is searchable or multi-layer, type/search, WAIT 2-3 seconds,
and keep clicking until the FINAL leaf option is selected and the field text
changes. Do NOT move on after the first click if a submenu appears or the
field still looks empty/invalid. Do NOT click a dropdown option and then
Save/Continue in the same action batch; wait briefly and re-evaluate first.

Other rules:
- {'Use the provided credentials to log in or create an account if needed. For Workday, fill email + password + confirm password on the Create Account page.' if sensitive_data else 'If a login wall appears, report it as a blocker.'}
- Do NOT click the final Submit button. Stop at the review page and use the done action.
- If anything pops up blocking the form, close it and continue.
{workday_start_flow_rules.rstrip()}
- Stay on this site — do NOT open new tabs or navigate away.
- After auth, continue from wherever the redirect lands — do NOT go back to the job URL.
"""

	async def _on_step_start(agent_instance):
		await install_same_tab_guard(agent_instance)
		try:
			pass
		except Exception:
			pass  # Non-fatal — best effort

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
		max_actions_per_step=settings.agent_max_actions_per_step,
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
		print(f"  LLM:       Direct API")
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
			in_tok = getattr(history.usage, 'total_prompt_tokens', None) or getattr(history.usage, 'total_input_tokens', 0)
			out_tok = getattr(history.usage, 'total_completion_tokens', None) or getattr(history.usage, 'total_output_tokens', 0)
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
