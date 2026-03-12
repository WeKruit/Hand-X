#!/usr/bin/env python3
"""Test Hand-X against a real job URL.

Usage:
    # Basic — opens visible browser, uses test resume
    python scripts/run_test.py "https://jobs.lever.co/example/12345"

    # With custom resume
    python scripts/run_test.py "https://company.wd5.myworkdayjobs.com/..." --resume path/to/resume.json

    # Headless mode
    python scripts/run_test.py "https://..." --headless

    # With credentials (for login-required ATSes)
    python scripts/run_test.py "https://..." --email you@email.com --password yourpass

    # Limit steps/budget
    python scripts/run_test.py "https://..." --max-steps 20 --max-budget 0.10

Requires:
    - ANTHROPIC_API_KEY or GH_ANTHROPIC_API_KEY set in env or .env file
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

# Load .env before any imports that read settings
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


async def main() -> None:
	parser = argparse.ArgumentParser(description="Test Hand-X against a job URL")
	parser.add_argument("url", help="Job posting URL to apply to")
	parser.add_argument("--resume", default=str(ROOT / "scripts/test_resume.json"), help="Path to resume JSON file")
	parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
	parser.add_argument("--max-steps", type=int, default=50, help="Max agent steps (default: 50)")
	parser.add_argument("--max-budget", type=float, default=1.00, help="Max LLM spend in USD (default: 1.00)")
	parser.add_argument("--email", help="Login email for the ATS")
	parser.add_argument("--password", help="Login password for the ATS")
	parser.add_argument("--platform", help="Force platform (workday/greenhouse/lever/smartrecruiters/generic)")
	args = parser.parse_args()

	# ── Check API key ─────────────────────────────────────────────
	api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GH_ANTHROPIC_API_KEY")
	if not api_key:
		print("ERROR: Set ANTHROPIC_API_KEY or GH_ANTHROPIC_API_KEY in your environment or .env file")
		sys.exit(1)

	# Force settings for test mode
	os.environ.setdefault("GH_ANTHROPIC_API_KEY", api_key)
	os.environ["GH_HEADLESS"] = str(args.headless).lower()
	os.environ["GH_MAX_STEPS_PER_JOB"] = str(args.max_steps)
	os.environ["GH_MAX_BUDGET_PER_JOB"] = str(args.max_budget)

	# ── Load resume ───────────────────────────────────────────────
	from ghosthands.integrations.resume_loader import load_resume_from_file
	resume_path = Path(args.resume)
	if not resume_path.exists():
		print(f"ERROR: Resume file not found: {resume_path}")
		sys.exit(1)

	resume_profile = load_resume_from_file(str(resume_path))
	print(f"Loaded resume: {resume_profile.get('full_name', 'Unknown')}")
	print(f"  Email: {resume_profile.get('email')}")
	print(f"  Skills: {', '.join(resume_profile.get('skills', [])[:5])}...")

	# ── Detect platform ───────────────────────────────────────────
	if args.platform:
		platform = args.platform
	else:
		from ghosthands.platforms import detect_platform
		platform = detect_platform(args.url)
	print(f"  Platform: {platform}")

	# ── Build credentials ─────────────────────────────────────────
	credentials = None
	if args.email and args.password:
		credentials = {"email": args.email, "password": args.password}
		print(f"  Credentials: {args.email} / {'*' * len(args.password)}")

	# ── Build task ────────────────────────────────────────────────
	task = (
		f"Go to {args.url} and fill out the job application using the applicant's "
		f"resume profile. Use domhand_fill for form fields. "
		f"Do NOT click the final Submit button — stop at the review page. "
		f"If you encounter a login wall, {'use the provided credentials to log in' if credentials else 'report it as a blocker'}."
	)

	print()
	print("=" * 60)
	print(f"  URL:        {args.url}")
	print(f"  Platform:   {platform}")
	print(f"  Max steps:  {args.max_steps}")
	print(f"  Max budget: ${args.max_budget:.2f}")
	print(f"  Headless:   {args.headless}")
	print("=" * 60)
	print()

	# ── Run agent ─────────────────────────────────────────────────
	from ghosthands.agent.factory import run_job_agent

	async def on_status(status: dict) -> None:
		step = status.get("step", "?")
		cost = status.get("cost_usd", 0)
		done = status.get("is_done", False)
		blocker = status.get("blocker")
		goal = status.get("next_goal", "")
		prefix = "DONE" if done else f"Step {step}"
		line = f"  [{prefix}] cost=${cost:.4f}"
		if blocker:
			line += f" BLOCKER: {blocker}"
		if goal:
			line += f" | {goal[:80]}"
		print(line)

	print("Starting agent...")
	print()

	result = await run_job_agent(
		task=task,
		resume_profile=resume_profile,
		credentials=credentials,
		platform=platform,
		headless=args.headless,
		max_steps=args.max_steps,
		job_id="test-local",
		max_budget=args.max_budget,
		on_status_update=on_status,
	)

	# ── Print result ──────────────────────────────────────────────
	print()
	print("=" * 60)
	print("  RESULT")
	print("=" * 60)
	print(f"  Success:   {result['success']}")
	print(f"  Steps:     {result['steps']}")
	print(f"  Cost:      ${result['cost_usd']:.4f}")
	if result.get("blocker"):
		print(f"  Blocker:   {result['blocker']}")
	if result.get("extracted_text"):
		print(f"  Output:    {result['extracted_text'][:200]}")
	print("=" * 60)


if __name__ == "__main__":
	asyncio.run(main())
