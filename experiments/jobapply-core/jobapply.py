"""AI job-application submitter — thin glue over upstream browser-use.

Design principle: **reuse browser-use's built-in features, build as little as
possible.** This file owns no replay engine, no cost/caching code, and no
verification stack. It only wires together features browser-use already ships:

    • form fill            -> browser_use.Agent (vanilla)
    • read email code      -> browser_use.integrations.gmail.register_gmail_actions
                              (the agent calls the get_recent_emails action itself)
    • "script cache"       -> Agent.save_history() + Agent.load_and_rerun(variables=…)
                              (deterministic steps replay with NO LLM call)
    • input-token caching  -> native (browser-use prices cache_read per model)
    • cost reporting       -> Agent(calculate_cost=True) -> history.usage
    • multi-page wizards    -> save_history/load_and_rerun record & replay the FULL
                              trajectory (every Next/Continue + navigation) and
                              variable substitution walks all pages

The only thing we author is (a) a reusable standing-instruction string and
(b) a small profile->variables mapping for reruns. Everything else is a call
into browser-use.

See README.md for setup and the environment caveats (needs BROWSER_USE_API_KEY
+ network to llm.api.browser-use.com and the ATS site).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

# NOTE: browser_use is imported lazily inside record()/replay()/_gmail_tools() so
# the offline logic (build_instructions, map_profile_to_variables) and its unit
# tests run with no browser-use install / no heavy deps.

HERE = Path(__file__).resolve().parent
DEFAULT_PROFILE = HERE / "fixtures" / "sample_profile.json"


# ─────────────────────────────────────────────────────────────────────────────
# (a) The one reusable artifact we author: a standing instruction block injected
# via Agent(extend_system_message=…), reused verbatim for every job. It only
# encodes conventions that ALIGN with browser-use's own operating manual
# (system_prompt.md) — multi-page advance, combobox handling, verification wall,
# resume upload — so we steer the agent without fighting its defaults.
# ─────────────────────────────────────────────────────────────────────────────
def build_instructions(submit: bool) -> str:
    final = (
        "When the whole application is complete, click the final Submit/Apply button to submit."
        if submit
        else "Do NOT click the final Submit/Apply button — stop on the review step and report what is filled."
    )
    return f"""You are filling out a multi-page job application.

- Use ONLY the applicant profile provided in the task. Never fabricate answers.
  If a required field has no matching profile value, choose the most reasonable
  non-committal option; leave optional fields blank.
- This is a multi-step wizard: after completing a page, click
  Next / Continue / Save and continue to advance to the next page.
- For autocomplete / react-select dropdowns: type the value, WAIT one step for
  the suggestion list, then click the matching suggestion (do not press Enter).
- Upload the resume from the available files when a file-upload field appears.
- If you hit an email-verification wall (a page asking for a code or magic link
  sent to the applicant's email), call the get_recent_emails action with a
  keyword like 'verification' or the company name, read the latest code/link,
  and enter it to continue. CAPTCHAs are solved automatically — just continue.
- On a Review/Confirm page, check the values, then {('submit.' if submit else 'stop.')}
- {final}"""


def _make_llm(model: str) -> Any:
    """Pick the chat model from the bare id — no proxy, no ghosthands routing."""
    if model.startswith("bu-") or model == "smart":
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


def _gmail_tools(access_token: str | None, credentials_file: str | None, token_file: str | None) -> Tools:
    """Reuse browser-use's Gmail integration verbatim — register the built-in
    get_recent_emails action so the agent can pull verification codes itself."""
    from browser_use import Tools

    tools = Tools()
    try:
        from browser_use.integrations.gmail import GmailService, register_gmail_actions

        if access_token:
            register_gmail_actions(tools, access_token=access_token)
        elif credentials_file or token_file:
            register_gmail_actions(
                tools, gmail_service=GmailService(credentials_file=credentials_file, token_file=token_file)
            )
        else:
            # No creds provided — still register; the action authenticates lazily
            # from the browser-use config dir (gmail_credentials.json/gmail_token.json).
            register_gmail_actions(tools)
    except Exception as exc:  # integration import/setup is optional for a fill-only run
        print(f"[warn] Gmail integration not registered ({type(exc).__name__}: {exc}); "
              "verification walls will not be auto-resolved.")
    return tools


def _build_task(job_url: str, profile: dict, resume: str | None) -> str:
    parts = [
        f"Open {job_url} and complete the job application end to end.",
        "Applicant profile (JSON):",
        json.dumps(profile, indent=2),
    ]
    if resume:
        parts.append(f"\nResume file available locally at: {resume}")
    return "\n".join(parts)


def _print_cost(label: str, history: Any) -> None:
    u = getattr(history, "usage", None)
    print(f"\n----- {label} -----")
    print(f"  done:  {history.is_done()}   steps: {len(history.history)}")
    if u is not None:
        print(f"  COST:  ${u.total_cost:.4f}")
        print(f"  prompt {u.total_prompt_tokens:,} (cached {u.total_prompt_cached_tokens:,}) "
              f"| completion {u.total_completion_tokens:,}")
    else:
        print("  (no usage recorded)")


# (b) The other small thing we author: map a new applicant profile onto the
# variable names browser-use auto-detected in the recorded history, so a rerun
# re-fills the same form with different data. detect_variables() does the hard
# part; we just match names/formats to profile keys.
def map_profile_to_variables(detected: dict[str, Any], profile: dict) -> dict[str, str]:
    aliases = {
        "email": ["email", "e-mail"],
        "first_name": ["first_name", "firstname", "given"],
        "last_name": ["last_name", "lastname", "surname", "family"],
        "full_name": ["full_name", "name"],
        "phone": ["phone", "tel", "mobile"],
        "linkedin": ["linkedin"],
        "github": ["github"],
        "website": ["website", "url", "portfolio"],
        "location": ["location", "city", "address"],
    }
    out: dict[str, str] = {}
    for var_name, info in detected.items():
        fmt = (getattr(info, "format", None) or "").lower()
        key = var_name.lower()
        for profile_key, needles in aliases.items():
            if profile_key not in profile:
                continue
            if any(n in key for n in needles) or fmt == profile_key:
                out[var_name] = str(profile[profile_key])
                break
    return out


async def record(args: argparse.Namespace) -> None:
    from browser_use import Agent, Browser, BrowserProfile

    profile = json.loads(Path(args.profile).read_text())
    resume = str(Path(args.resume).resolve()) if args.resume else None

    tools = _gmail_tools(args.gmail_access_token, args.gmail_credentials, args.gmail_token)
    agent = Agent(
        task=_build_task(args.job_url, profile, resume),
        llm=_make_llm(args.model),
        tools=tools,
        browser=Browser(browser_profile=BrowserProfile(headless=args.headless, keep_alive=True)),
        extend_system_message=build_instructions(args.submit),
        available_file_paths=[resume] if resume else None,
        use_vision="auto",
        max_actions_per_step=5,   # chain input+input+…+click -> fewer steps -> cheaper
        calculate_cost=True,      # native cost tracking -> history.usage
    )

    print(f"[record] model={args.model} submit={args.submit} url={args.job_url}")
    history = await agent.run(max_steps=args.max_steps)
    _print_cost("RECORD", history)

    # Save the cached "script" (full multi-page trajectory) + the detected vars
    # sidecar so a later rerun knows which fields are substitutable.
    agent.save_history(args.history)
    try:
        detected = agent.detect_variables()
        sidecar = Path(args.history).with_suffix(".vars.json")
        sidecar.write_text(json.dumps(
            {n: {"original_value": getattr(v, "original_value", ""),
                 "format": getattr(v, "format", None)} for n, v in detected.items()},
            indent=2,
        ))
        print(f"[record] saved history -> {args.history}; {len(detected)} variable(s) -> {sidecar.name}")
    except Exception as exc:
        print(f"[record] saved history -> {args.history}; variable detection skipped ({exc})")

    if not args.headless:
        await _hold_open(agent)


async def replay(args: argparse.Namespace) -> None:
    """Cheap re-submit: same job page, different applicant data. Deterministic
    fills replay with no LLM; the recorded get_recent_emails step re-evaluates
    live so a fresh verification code is fetched automatically."""
    from browser_use import Agent, Browser, BrowserProfile

    profile = json.loads(Path(args.profile).read_text())

    # Build the substitution dict from the sidecar produced at record time.
    variables: dict[str, str] = {}
    sidecar = Path(args.history).with_suffix(".vars.json")
    if sidecar.exists():
        detected_raw = json.loads(sidecar.read_text())
        # adapt to the shape map_profile_to_variables expects
        class _V:  # tiny shim so we reuse the same mapper
            def __init__(self, d): self.format = d.get("format")
        variables = map_profile_to_variables({n: _V(d) for n, d in detected_raw.items()}, profile)
        print(f"[replay] substituting {len(variables)} field(s): {list(variables)}")
    else:
        print("[replay] no .vars.json sidecar found — replaying with original recorded data")

    agent = Agent(
        task="",  # rerun drives from history, not a task
        llm=_make_llm(args.model),
        tools=_gmail_tools(args.gmail_access_token, args.gmail_credentials, args.gmail_token),
        browser=Browser(browser_profile=BrowserProfile(headless=args.headless, keep_alive=True)),
        calculate_cost=True,
    )
    print(f"[replay] model={args.model} url-from-history={args.history}")
    await agent.load_and_rerun(args.history, variables=variables or None)

    # load_and_rerun populates the same usage tracker
    usage = await agent.token_cost_service.get_usage_summary()
    print("\n----- REPLAY -----")
    print(f"  COST:  ${usage.total_cost:.4f}")
    print(f"  prompt {usage.total_prompt_tokens:,} (cached {usage.total_prompt_cached_tokens:,}) "
          f"| completion {usage.total_completion_tokens:,}")
    print("  (deterministic fills cost $0 LLM; cost is the live email-read step + final summary)")
    if not args.headless:
        await _hold_open(agent)


async def _hold_open(agent: Any) -> None:
    print("\n  Browser left open for review. Ctrl+C to close.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if agent.browser_session is not None:
            await agent.browser_session.kill()


def main() -> None:
    p = argparse.ArgumentParser(description="Job-application submitter (glue over browser-use)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", default="bu-2-0", help="Model id (default: bu-2-0)")
        sp.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Applicant profile JSON")
        sp.add_argument("--history", default="application_history.json", help="Saved trajectory path")
        sp.add_argument("--headless", action="store_true")
        sp.add_argument("--gmail-access-token", default=None, help="Gmail OAuth access token (read-only)")
        sp.add_argument("--gmail-credentials", default=None, help="Gmail OAuth client credentials JSON")
        sp.add_argument("--gmail-token", default=None, help="Gmail OAuth token JSON")

    pr = sub.add_parser("record", help="Run + record the application trajectory")
    common(pr)
    pr.add_argument("--job-url", required=True)
    pr.add_argument("--resume", default=None)
    pr.add_argument("--max-steps", type=int, default=80)
    pr.add_argument("--submit", action="store_true", help="Actually submit (IRREVERSIBLE). Default: fill only.")

    rp = sub.add_parser("replay", help="Cheaply re-run a saved trajectory with new data")
    common(rp)

    args = p.parse_args()
    asyncio.run(record(args) if args.cmd == "record" else replay(args))


if __name__ == "__main__":
    main()
