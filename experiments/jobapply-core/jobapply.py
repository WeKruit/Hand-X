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

- Use ONLY the applicant resume + profile data provided in the task. Never
  fabricate answers. Fill work experience, education, dates, and open-ended
  questions from `experience`, `education`, `summary`, and `cover_letter`. If a
  required field has detail not in the JSON, read the resume file for it; if it's
  genuinely unknown, choose the most reasonable non-committal option and leave
  optional fields blank.
- For voluntary / EEO / demographic questions (gender, race, veteran, disability),
  use the values in `eeo_optional`, defaulting to "Prefer not to say".
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
        "",
        "Applicant resume + profile data (JSON) — use this as the source of truth "
        "for every field, including work experience, education, dates, skills, and "
        "open-ended questions (draw on `summary`, `experience[].highlights`, and "
        "`cover_letter`):",
        json.dumps(profile, indent=2),
    ]
    if resume:
        parts.append(
            f"\nResume file is available locally at: {resume}\n"
            "Upload it when a resume/CV file-upload field appears. If a field needs "
            "detail not in the JSON above, read the resume file for it."
        )
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


def _write_vars_sidecar(agent: Any, history_path: str) -> int:
    """Persist the fields browser-use auto-detected as substitutable, so a later
    rerun knows what it can swap. Returns the count (0 on failure)."""
    try:
        detected = agent.detect_variables()
        sidecar = Path(history_path).with_suffix(".vars.json")
        sidecar.write_text(json.dumps(
            {n: {"original_value": getattr(v, "original_value", ""),
                 "format": getattr(v, "format", None)} for n, v in detected.items()},
            indent=2,
        ))
        return len(detected)
    except Exception as exc:
        print(f"[record] variable detection skipped ({exc})")
        return 0


async def _kill(agent: Any) -> None:
    try:
        if agent.browser_session is not None:
            await agent.browser_session.kill()
    except Exception:
        pass


async def _do_record(args: argparse.Namespace, *, model: str, history_path: str, submit: bool) -> tuple[Any, Any]:
    """Core record run. Returns (history, agent); caller owns browser lifecycle."""
    from browser_use import Agent, Browser, BrowserProfile

    profile = json.loads(Path(args.profile).read_text())
    resume = str(Path(args.resume).resolve()) if args.resume else None

    agent = Agent(
        task=_build_task(args.job_url, profile, resume),
        llm=_make_llm(model),
        tools=_gmail_tools(args.gmail_access_token, args.gmail_credentials, args.gmail_token),
        browser=Browser(browser_profile=BrowserProfile(headless=args.headless, keep_alive=True)),
        extend_system_message=build_instructions(submit),
        available_file_paths=[resume] if resume else None,
        use_vision="auto",
        max_actions_per_step=5,   # chain input+input+…+click -> fewer steps -> cheaper
        calculate_cost=True,      # native cost tracking -> history.usage
    )
    history = await agent.run(max_steps=args.max_steps)
    agent.save_history(history_path)  # the cached "script" (full multi-page trajectory)
    return history, agent


async def record(args: argparse.Namespace) -> None:
    print(f"[record] model={args.model} submit={args.submit} url={args.job_url}")
    history, agent = await _do_record(args, model=args.model, history_path=args.history, submit=args.submit)
    _print_cost("RECORD", history)
    n = _write_vars_sidecar(agent, args.history)
    print(f"[record] saved history -> {args.history}; {n} substitutable variable(s) -> "
          f"{Path(args.history).with_suffix('.vars.json').name}")
    if args.headless:
        await _kill(agent)
    else:
        await _hold_open(agent)


async def compare(args: argparse.Namespace) -> None:
    """Run the SAME job through multiple models and print a side-by-side cost table.
    Fill-only (never submits). Each run saves its own history."""
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    rows: list[tuple[str, Any, str]] = []
    for model in models:
        hp = f"compare_{model.replace('/', '_').replace('.', '_')}.json"
        print(f"\n[compare] ===== {model} =====")
        try:
            history, agent = await _do_record(args, model=model, history_path=hp, submit=False)
            _print_cost(model, history)
            _write_vars_sidecar(agent, hp)
            await _kill(agent)  # close between runs so windows don't stack
            rows.append((model, history, hp))
        except Exception as exc:
            print(f"[compare] {model} failed: {type(exc).__name__}: {exc}")
            rows.append((model, None, hp))

    print("\n" + "=" * 72)
    print(f"  COST COMPARISON — {args.job_url}")
    print("=" * 72)
    print(f"  {'model':<28} {'steps':>6} {'done':>5} {'cost':>10}   history")
    for model, history, hp in rows:
        if history is None:
            print(f"  {model:<28} {'—':>6} {'—':>5} {'FAILED':>10}")
            continue
        u = getattr(history, "usage", None)
        cost = f"${u.total_cost:.4f}" if u else "n/a"
        print(f"  {model:<28} {len(history.history):>6} {str(history.is_done()):>5} {cost:>10}   {hp}")
    print("=" * 72)
    print("  (fill-only; rerun any saved history cheaply with:  python jobapply.py replay --history <file>)")


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
        # Non-empty apply-flow task on purpose: browser-use gates value-pattern
        # variable detection on whether the task looks like a job application
        # (Agent._is_apply_flow_task). The record run's task is apply-flow, so we
        # must match it here — otherwise replay re-detects variable names under a
        # different policy than the saved .vars.json and substitutions silently skip.
        task="Re-run the saved job application with substituted applicant data.",
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
    if args.headless:
        await _kill(agent)
    else:
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
    # Local convenience: pick up a .env (BROWSER_USE_API_KEY, GOOGLE_API_KEY, …) if present.
    try:
        from dotenv import load_dotenv

        load_dotenv(HERE / ".env")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="Job-application submitter (glue over browser-use)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Applicant profile JSON")
        sp.add_argument("--history", default="application_history.json", help="Saved trajectory path")
        sp.add_argument("--headless", action="store_true")
        sp.add_argument("--gmail-access-token", default=None, help="Gmail OAuth access token (read-only)")
        sp.add_argument("--gmail-credentials", default=None, help="Gmail OAuth client credentials JSON")
        sp.add_argument("--gmail-token", default=None, help="Gmail OAuth token JSON")

    def job_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--job-url", required=True)
        sp.add_argument("--resume", default=None)
        sp.add_argument("--max-steps", type=int, default=80)

    pr = sub.add_parser("record", help="Run + record the application trajectory")
    common(pr)
    job_args(pr)
    pr.add_argument("--model", default="bu-2-0", help="Model id (default: bu-2-0)")
    pr.add_argument("--submit", action="store_true", help="Actually submit (IRREVERSIBLE). Default: fill only.")

    rp = sub.add_parser("replay", help="Cheaply re-run a saved trajectory with new data")
    common(rp)
    rp.add_argument("--model", default="bu-2-0", help="Model id (default: bu-2-0)")

    cp = sub.add_parser("compare", help="Run the same job on multiple models; print a cost table")
    common(cp)
    job_args(cp)
    cp.add_argument("--models", default="bu-2-0,gemini-3-flash-preview",
                    help="Comma-separated model ids (default: bu-2-0,gemini-3-flash-preview)")

    args = p.parse_args()
    fn = {"record": record, "replay": replay, "compare": compare}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
