#!/usr/bin/env python3
"""Quick test suite for Hand-X — run without arguments to validate everything.

Usage:
    # Run all offline tests (no API key needed)
    python test_quick.py

    # Run with a live browser (no API key needed, just opens + extracts)
    python test_quick.py --browser

    # Run full agent against a job URL (needs ANTHROPIC_API_KEY)
    python test_quick.py --live --job-url "https://job-boards.greenhouse.io/starburst/jobs/5123053008"

    # Run full agent with default Greenhouse URL
    python test_quick.py --live
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ═══════════════════════════════════════════════════════════════════════
# LEVEL 1: Offline tests (no browser, no API key)
# ═══════════════════════════════════════════════════════════════════════

def test_imports():
	"""Verify all 32 modules import."""
	failures = []
	modules = [
		"ghosthands.config.settings",
		"ghosthands.config.models",
		"ghosthands.dom.views",
		"ghosthands.dom.shadow_helpers",
		"ghosthands.dom.field_extractor",
		"ghosthands.dom.option_discovery",
		"ghosthands.dom.label_resolver",
		"ghosthands.dom.validation_reader",
		"ghosthands.actions.views",
		"ghosthands.actions.domhand_fill",
		"ghosthands.actions.domhand_select",
		"ghosthands.actions.domhand_upload",
		"ghosthands.actions.domhand_expand",
		"ghosthands.actions",
		"ghosthands.agent.factory",
		"ghosthands.agent.prompts",
		"ghosthands.agent.hooks",
		"ghosthands.platforms",
		"ghosthands.platforms.workday",
		"ghosthands.platforms.greenhouse",
		"ghosthands.platforms.lever",
		"ghosthands.platforms.smartrecruiters",
		"ghosthands.platforms.generic",
		"ghosthands.integrations.database",
		"ghosthands.integrations.credentials",
		"ghosthands.integrations.valet_callback",
		"ghosthands.integrations.resume_loader",
		"ghosthands.security.blocker_detector",
		"ghosthands.security.domain_lockdown",
		"ghosthands.worker.cost_tracker",
		"ghosthands.worker.hitl",
		"ghosthands.worker.executor",
	]
	for mod in modules:
		try:
			__import__(mod)
		except Exception as e:
			failures.append((mod, str(e)))
	return failures


def test_platform_detection():
	"""Verify platform URL detection."""
	from ghosthands.platforms import detect_platform

	cases = [
		("https://company.wd5.myworkdayjobs.com/en-US/External/job/NYC/SWE_12345", "workday"),
		("https://wd3.myworkday.com/company/d/task/12345.htmld", "workday"),
		("https://boards.greenhouse.io/company/jobs/12345", "greenhouse"),
		("https://job-boards.greenhouse.io/starburst/jobs/5123053008", "greenhouse"),
		("https://jobs.lever.co/company/12345-abcd", "lever"),
		("https://jobs.smartrecruiters.com/company/12345", "smartrecruiters"),
		("https://random-ats.example.com/apply/12345", "generic"),
	]
	failures = []
	for url, expected in cases:
		result = detect_platform(url)
		if result != expected:
			failures.append(f"{url}: expected={expected}, got={result}")
	return failures


def test_domain_lockdown():
	"""Verify domain allowlisting."""
	from ghosthands.security.domain_lockdown import is_url_allowed

	cases = [
		("https://wd5.myworkdayjobs.com/job/apply", "workday", True),
		("https://evil.com/phish", "workday", False),
		("https://boards.greenhouse.io/apply", "greenhouse", True),
		("https://jobs.lever.co/apply", "lever", True),
		("https://cdn.jsdelivr.net/script.js", "workday", True),  # CDN always allowed
		("https://malware.ru/bad", "generic", False),
	]
	failures = []
	for url, platform, expected in cases:
		result = is_url_allowed(url, platform)
		if result != expected:
			failures.append(f"{url} ({platform}): expected={expected}, got={result}")
	return failures


def test_cost_estimation():
	"""Verify LLM cost estimation."""
	from ghosthands.config.models import estimate_cost

	cost = estimate_cost("claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=500)
	if not (0.001 < cost < 0.01):
		return [f"Haiku cost unexpected: {cost}"]

	cost2 = estimate_cost("claude-sonnet-4-20250514", input_tokens=1000, output_tokens=500)
	if not (cost2 > cost):
		return [f"Sonnet should cost more than Haiku: sonnet={cost2}, haiku={cost}"]
	return []


def test_resume_loader():
	"""Verify resume loading from JSON file."""
	from ghosthands.integrations.resume_loader import load_resume_from_file

	sample = ROOT / "examples" / "apply_to_job_sample_data.json"
	if not sample.exists():
		return ["Sample data file missing"]

	profile = load_resume_from_file(str(sample))
	if not profile.get("first_name") and not profile.get("email"):
		return [f"Profile missing basic fields: {list(profile.keys())[:5]}"]
	return []


def test_action_registration():
	"""Verify DomHand actions register on browser-use Tools."""
	from browser_use import Tools
	from ghosthands.actions import register_domhand_actions

	tools = Tools()
	register_domhand_actions(tools)

	expected = {"domhand_fill", "domhand_select", "domhand_upload", "domhand_expand"}
	registered = {n for n in tools.registry.registry.actions if n.startswith("domhand_")}
	missing = expected - registered
	if missing:
		return [f"Missing actions: {missing}"]
	return []


def test_system_prompt():
	"""Verify system prompt builds without error."""
	from ghosthands.agent.prompts import build_system_prompt

	profile = {"first_name": "Test", "email": "test@example.com", "skills": ["Python"]}
	prompt = build_system_prompt(profile, "workday")
	if len(prompt) < 100:
		return [f"Prompt too short: {len(prompt)} chars"]
	if "domhand_fill" not in prompt:
		return ["Prompt missing domhand_fill reference"]
	return []


def test_workday_config():
	"""Verify Workday platform config is complete."""
	from ghosthands.platforms.workday import WORKDAY_CONFIG, AUTH_STATES

	failures = []
	if len(AUTH_STATES) < 5:
		failures.append(f"Expected 5+ auth states, got {len(AUTH_STATES)}")
	if WORKDAY_CONFIG.form_strategy != "dom_first":
		failures.append(f"Expected dom_first strategy, got {WORKDAY_CONFIG.form_strategy}")
	if len(WORKDAY_CONFIG.guardrails) < 5:
		failures.append(f"Expected 5+ guardrails, got {len(WORKDAY_CONFIG.guardrails)}")
	return failures


# ═══════════════════════════════════════════════════════════════════════
# LEVEL 2: Browser test (opens browser, no API key needed)
# ═══════════════════════════════════════════════════════════════════════

async def test_browser_extraction():
	"""Open a browser, navigate to a form page, extract fields with DomHand."""
	from playwright.async_api import async_playwright
	from ghosthands.dom.field_extractor import extract_form_fields
	from ghosthands.dom.shadow_helpers import inject_helpers

	# Use a simple public form for testing
	test_url = "https://httpbin.org/forms/post"

	print(f"\n  Opening browser → {test_url}")
	async with async_playwright() as p:
		browser = await p.chromium.launch(headless=True)
		page = await browser.new_page()
		await page.goto(test_url, wait_until="domcontentloaded", timeout=15000)

		print("  Injecting __ff helpers...")
		await inject_helpers(page)

		has_ff = await page.evaluate("!!window.__ff")
		if not has_ff:
			await browser.close()
			return ["__ff helpers not injected"]

		print("  Extracting form fields...")
		result = await extract_form_fields(page)

		await browser.close()

		failures = []
		if len(result.fields) == 0:
			failures.append("No fields extracted from httpbin form")
		else:
			print(f"  Extracted {len(result.fields)} fields:")
			for f in result.fields[:10]:
				print(f"    - [{f.field_type}] {f.label or f.name or '(no label)'} {'*' if f.required else ''}")

		return failures


# ═══════════════════════════════════════════════════════════════════════
# LEVEL 3: Live agent test (needs API key + browser)
# ═══════════════════════════════════════════════════════════════════════

async def test_live_agent(job_url: str, max_steps: int = 15):
	"""Run the full agent against a real job URL."""
	api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GH_ANTHROPIC_API_KEY")
	if not api_key:
		return ["ANTHROPIC_API_KEY not set — skipping live test"]

	os.environ.setdefault("GH_ANTHROPIC_API_KEY", api_key)
	os.environ["GH_HEADLESS"] = "false"  # visible browser for testing

	from ghosthands.integrations.resume_loader import load_resume_from_file
	from ghosthands.platforms import detect_platform

	sample = ROOT / "examples" / "apply_to_job_sample_data.json"
	resume = ROOT / "examples" / "resume.pdf"

	if not sample.exists():
		return ["examples/apply_to_job_sample_data.json missing"]
	if not resume.exists():
		return ["examples/resume.pdf missing"]

	profile = load_resume_from_file(str(sample))
	platform = detect_platform(job_url)

	print(f"\n  URL:      {job_url}")
	print(f"  Platform: {platform}")
	print(f"  Steps:    {max_steps}")

	from browser_use import Agent, Browser, BrowserProfile, ChatAnthropic, Tools
	from ghosthands.actions import register_domhand_actions
	from ghosthands.agent.prompts import build_system_prompt

	llm = ChatAnthropic(model="claude-sonnet-4-0", api_key=api_key)
	tools = Tools()
	register_domhand_actions(tools)

	browser = Browser(browser_profile=BrowserProfile(headless=False))

	task = f"""Go to {job_url} and fill out the job application.
Applicant: {json.dumps(profile, indent=2)}
Use domhand_fill for form fields. Upload resume from {resume}.
Do NOT submit. Stop at review page."""

	agent = Agent(
		task=task,
		llm=llm,
		browser=browser,
		tools=tools,
		extend_system_message=build_system_prompt(profile, platform),
		available_file_paths=[str(resume)],
		use_vision=True,
		max_actions_per_step=5,
	)

	print("  Running agent...\n")
	history = await agent.run(max_steps=max_steps)

	steps = len(history.history)
	cost = history.usage.total_cost if history.usage else 0
	done = history.is_done()

	print(f"\n  Done:  {done}")
	print(f"  Steps: {steps}")
	print(f"  Cost:  ${cost:.4f}")

	result = history.final_result()
	if result:
		print(f"  Output: {result[:300]}")

	return []


# ═══════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════

def run_test(name: str, fn):
	"""Run a test function and print pass/fail."""
	t0 = time.time()
	try:
		failures = fn()
	except Exception as e:
		failures = [f"EXCEPTION: {e}"]
	dt = time.time() - t0

	if failures:
		print(f"  FAIL  {name} ({dt:.1f}s)")
		for f in failures:
			print(f"        {f}")
		return False
	else:
		print(f"  PASS  {name} ({dt:.1f}s)")
		return True


async def run_async_test(name: str, fn, *args):
	t0 = time.time()
	try:
		failures = await fn(*args)
	except Exception as e:
		import traceback
		failures = [f"EXCEPTION: {e}\n{traceback.format_exc()}"]
	dt = time.time() - t0

	if failures:
		print(f"  FAIL  {name} ({dt:.1f}s)")
		for f in failures:
			print(f"        {f}")
		return False
	else:
		print(f"  PASS  {name} ({dt:.1f}s)")
		return True


async def main():
	parser = argparse.ArgumentParser(description="Hand-X quick test suite")
	parser.add_argument("--browser", action="store_true", help="Include browser extraction test")
	parser.add_argument("--live", action="store_true", help="Include live agent test (needs API key)")
	parser.add_argument("--job-url", default="https://job-boards.greenhouse.io/starburst/jobs/5123053008")
	parser.add_argument("--max-steps", type=int, default=15, help="Max steps for live test")
	args = parser.parse_args()

	print()
	print("=" * 60)
	print("  Hand-X Quick Test Suite")
	print("=" * 60)

	# Level 1: Offline
	print()
	print("  LEVEL 1: Offline (no browser, no API key)")
	print("  " + "-" * 40)

	results = []
	results.append(run_test("imports (32 modules)", test_imports))
	results.append(run_test("platform detection", test_platform_detection))
	results.append(run_test("domain lockdown", test_domain_lockdown))
	results.append(run_test("cost estimation", test_cost_estimation))
	results.append(run_test("resume loader", test_resume_loader))
	results.append(run_test("action registration", test_action_registration))
	results.append(run_test("system prompt", test_system_prompt))
	results.append(run_test("workday config", test_workday_config))

	# Level 2: Browser
	if args.browser or args.live:
		print()
		print("  LEVEL 2: Browser (headless extraction)")
		print("  " + "-" * 40)
		results.append(await run_async_test("DOM field extraction", test_browser_extraction))

	# Level 3: Live
	if args.live:
		print()
		print("  LEVEL 3: Live Agent")
		print("  " + "-" * 40)
		results.append(await run_async_test("live agent", test_live_agent, args.job_url, args.max_steps))

	# Summary
	passed = sum(results)
	total = len(results)
	print()
	print("=" * 60)
	status = "ALL PASSED" if passed == total else f"{total - passed} FAILED"
	print(f"  {passed}/{total} tests passed — {status}")
	print("=" * 60)
	print()

	sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
	asyncio.run(main())
