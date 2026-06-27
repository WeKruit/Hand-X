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
        "When every field is filled and verified, click the final Submit/Apply button once to submit, then call done."
        if submit
        else (
            "STOP PROTOCOL — the moment every visible field is filled and the ONLY remaining\n"
            "  control is the final Submit / Submit application / Apply / Finish button, IMMEDIATELY\n"
            "  call done(success=True) with a short summary of what you filled. That is your terminal\n"
            "  action. It is an ABSOLUTE rule that you NEVER click Submit/Apply/Finish — not to submit,\n"
            "  not to 'advance', not to 'check', not to reveal errors. After you reach the Submit step\n"
            "  do NOT go back to re-verify or re-fill ANY field — clicking Submit only creates\n"
            "  validation errors you will then loop on. Filled + at Submit = call done NOW."
        )
    )
    verification = (
        "- If you hit an email-verification wall (a page asking for a code or magic link\n"
        "  sent to the applicant's email), call the get_recent_emails action with a\n"
        "  keyword like 'verification' or the company name, read the latest code/link,\n"
        "  and enter it to continue. CAPTCHAs are solved automatically — just continue."
        if submit else
        "- If you reach an email-verification wall (a page asking for a code / magic link\n"
        "  sent to the applicant's email), STOP THERE — the form is already filled and the\n"
        "  code is a submit-time gate. In this fill-only run do NOT call get_recent_emails\n"
        "  or wait for a code; report the application as filled up to the verification step.\n"
        "  CAPTCHAs are solved automatically — just continue past those."
    )
    return f"""You are filling out a multi-page job application.

- Use ONLY the applicant resume + profile data in the task. Never fabricate answers.
  That JSON is the parsed resume and has everything you need — fill work experience,
  education, dates, and open-ended questions from `experience`, `education`,
  `summary`, and `cover_letter`. Do NOT open or read the resume PDF (that loads it
  into context and wastes tokens). If a field is genuinely unknown, choose the most
  reasonable non-committal option and leave optional fields blank.
- For voluntary / EEO / demographic questions (gender, race, veteran, disability),
  use the values in `eeo_optional`, defaulting to "Prefer not to say".
- This is a multi-step wizard: after completing a page, click
  Next / Continue / Save and continue to advance to the next page.
- ONE action at a time, then OBSERVE. After each input/click, look at the fresh
  screenshot and confirm it actually took effect before the next action. ATS forms
  re-render and shift element indices, so a chained second action often lands on the
  wrong field — fill, observe, then fill the next.
- THE SCREENSHOT IS GROUND TRUTH, not the DOM/state text. Custom widgets (react-select,
  Greenhouse/Oracle/Workday selects, masked tel inputs) routinely read back EMPTY in
  the state even when the value is visibly present on screen. If a field VISUALLY shows
  the correct value and has no red error, it IS filled — move on. Do not re-type a field
  just because the state text looks empty; that false-empty is the #1 cause of the
  retype loop (e.g. phone typed once but state says empty -> do NOT retype 20 times).
- DROPDOWNS — USE YOUR DROPDOWN TOOLS, don't hand-click options. For any select /
  dropdown / Yes-No / single-choice field, FIRST call get_dropdown_options on it to
  read the real option list, then select_dropdown with the EXACT option text. That
  handles native <select> AND ARIA menus reliably in one step — always prefer it over
  manual click+type, and it never loops.
- TYPEAHEAD / react-select (autocomplete with NO native <select> — Location, School,
  Degree, Discipline, phone country/type — where select_dropdown does not apply): do
  the click->wait->observe->search->click routine. Click the control, type the value,
  call the wait action (2-3s) so the option list renders, OBSERVE it, then click the
  matching role=option (never press Enter to pick). If no options appear, retype a
  SHORTER / alternate term ("United States of America" -> "United States" -> "US";
  "+1"/"USA"/"Mobile" for phone). VERIFY a selected chip/value is visible before moving
  on — that, not the typed text, means it committed. Never batch the option-click with
  a Next/Continue in the same step.
- COMMIT a stuck value instead of re-typing it: if a field visibly holds the right text
  but still shows a red "required"/validation error, focus it and press Tab (or Enter)
  to commit — do NOT re-type it and do NOT refresh the whole page. After any typed date,
  Tab/blur away so the form re-validates.
- BEFORE ADVANCING a page, scan the screenshot: do not click Next/Continue while red
  errors, empty required fields, or a disabled Next button are visible — resolve those
  first. Stay near the current section; don't jump back to re-verify filled fields.
- RETRY → THEN LOOK (the anti-loop rule, this matters most). The browser-state `value=`
  for inputs is UNRELIABLE on React/Greenhouse/Workday forms — it reads EMPTY even when
  the field is filled and visible. So do NOT re-type a field just because the state
  shows it empty. Count your attempts per field: after you have typed the SAME field
  TWICE and the state still shows empty, STOP typing it and call the verify_field_visually
  action with the field's label — a cheap vision model looks at the screenshot and reports
  whether it is filled.
  - If verify_field_visually says filled (or you can plainly see it) → it IS filled. Mark
    it done and NEVER touch that field again.
  - Only if it reports clearly empty is it truly unfilled → try ONE different method
    (e.g. send_keys / click-then-type) or flag it as a blocker.
  Vision is the arbiter, never the state read-back. Re-typing a visibly-filled field is
  the #1 stuck loop. Selects/dropdowns are the same — a chosen option can read back
  empty while visibly selected; confirm by screenshot, do not re-select.
- ADVANCE-TO-VERIFY: on a multi-page form, the cleanest proof a page is complete is that
  Next/Continue ACCEPTS it — if the form advances, every required field was filled. If it
  blocks with an error, fix only the field it flags. Don't re-verify by re-reading state.
- CLICK-TO-REVEAL fields: some inputs are hidden behind a toggle/button — e.g. a
  cover letter or long-answer may show "Enter manually" / "Write" / "Paste" before
  the textarea exists. Click that control FIRST to reveal the field, THEN type.
- REPEATERS (sections holding MULTIPLE entries — Work Experience, Education, Skills,
  Languages, Certifications — usually with an "Add another" / "+ Add" button) are a
  legitimate multi-pass loop, NOT a stuck loop. Fully fill the current entry (commit
  its comboboxes per the rule above), THEN click "Add another" to open a fresh blank
  entry and fill the next item. Repeat once per item in the profile list — add ALL of
  `experience`, ALL of `education`, every `skills` value. Each pass must make progress
  (a NEW entry appears); stop when the profile list is exhausted and never add empty
  entries. Re-typing the same field with no new entry is the stuck loop to avoid;
  clicking "Add another" to enter the NEXT item is expected and correct.
- Upload the resume from the available files when a file-upload field appears.
{verification}
- reCAPTCHA / HUMAN VERIFICATION: a simple "I'm not a robot" checkbox is solved
  automatically — continue. But if an IMAGE reCAPTCHA / puzzle / challenge, or any
  human-verification step you cannot pass deterministically appears, do NOT attempt it
  and do NOT loop — call done(success=False) noting that human-in-the-loop (HITL)
  verification is required, and stop there.
- {final}"""


# Inter-action timing. ATS comboboxes/autocomplete (Workday, react-select) need a
# beat between "type the value" and "click the suggestion": the suggestion list is
# rendered by an XHR that the next action must not race. These widen browser-use's
# defaults (wait_between_actions=0.1, wait_for_network_idle=0.5, min_page_load=0.25)
# so dropdowns settle before the click — trades a little speed for click-wait-search
# reliability on multi-page wizards.
_BROWSER_TIMING = dict(
    wait_between_actions=1.0,                    # pause after every action (type -> suggestions -> click)
    wait_for_network_idle_page_load_time=1.0,    # let XHR-driven dropdowns / page transitions settle
    minimum_wait_page_load_time=0.5,             # floor wait after a navigation before reading the DOM
)


def _browser(headless: bool) -> Any:
    """Build the browser with dropdown-friendly timing (see _BROWSER_TIMING)."""
    from browser_use import Browser, BrowserProfile

    return Browser(browser_profile=BrowserProfile(headless=headless, keep_alive=True, **_BROWSER_TIMING))


def _trace_path(args: argparse.Namespace, model: str) -> str | None:
    """Per-(command, model) directory for browser-use's built-in cause trace
    (Agent.save_conversation_path): one file per step with the model's eval / goal /
    memory / chosen actions — i.e. WHY each step happened. Reuse-first: no custom
    tracing, just point the existing flag at a tidy per-run dir. '' disables it."""
    trace = (getattr(args, "trace", "") or "").strip()
    if not trace:
        return None
    return str(Path(trace) / args.cmd / model.replace("/", "_").replace(".", "_"))


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


def _gmail_tools(access_token: str | None, credentials_file: str | None, token_file: str | None) -> Any:
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


def _build_task(job_url: str, profile: dict, resume: str | None, submit: bool = False) -> str:
    objective = (
        f"Open {job_url} and complete the job application end to end, then submit it."
        if submit
        else f"Open {job_url} and FILL OUT the application completely, but STOP at the final "
        "Submit step WITHOUT clicking it — this is a fill-only run, never submit."
    )
    parts = [
        objective,
        "",
        "Applicant resume + profile data (JSON) — use this as the source of truth "
        "for every field, including work experience, education, dates, skills, and "
        "open-ended questions (draw on `summary`, `experience[].highlights`, and "
        "`cover_letter`):",
        json.dumps(profile, indent=2),
    ]
    if resume:
        parts.append(
            f"\nResume PDF available locally at: {resume}\n"
            "Upload it (file picker) when a resume/CV upload field appears. Do NOT "
            "read its contents — the JSON above is the parsed resume and has "
            "everything; reading the PDF into context wastes tokens."
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
    from browser_use import Agent

    profile = json.loads(Path(args.profile).read_text())
    resume = str(Path(args.resume).resolve()) if args.resume else None

    from vision_verify import register_visual_verify

    tools = _gmail_tools(args.gmail_access_token, args.gmail_credentials, args.gmail_token)
    register_visual_verify(tools)  # (c) cheap-VLM visual field check the agent can call on a stuck field

    agent = Agent(
        task=_build_task(args.job_url, profile, resume, submit=submit),
        llm=_make_llm(model),
        tools=tools,
        browser=_browser(args.headless),
        extend_system_message=build_instructions(submit),
        available_file_paths=[resume] if resume else None,
        use_vision="auto",
        vision_detail_level="low",  # (b) screenshots at low detail -> far fewer image tokens
        save_conversation_path=_trace_path(args, model),  # built-in per-step cause trace
        max_actions_per_step=args.max_actions,  # default 1 = act-then-observe. Chaining on
                                  # ATS forms lands later actions on stale element indices
                                  # (DOM re-renders after a combobox/upload) -> fields read
                                  # back empty -> retype loops. Raise for speed on simple forms.
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
        print(f"  {model:<28} {len(history.history):>6} {history.is_done()!s:>5} {cost:>10}   {hp}")
    print("=" * 72)
    print("  (fill-only; rerun any saved history cheaply with:  python jobapply.py replay --history <file>)")


async def replay(args: argparse.Namespace) -> None:
    """Cheap re-submit: same job page, different applicant data. Deterministic
    fills replay with no LLM; the recorded get_recent_emails step re-evaluates
    live so a fresh verification code is fetched automatically."""
    from browser_use import Agent

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
        # browser-use re-detects substitutable variables from the history AT REPLAY
        # time (service.py _substitute_variables_in_history) and only swaps a value
        # when replay-time detection re-derives the same var_name saved in .vars.json
        # at record time; unknown names are logged-and-skipped. So a stable, non-empty
        # task keeps detection consistent between record and replay. (NB: browser-use
        # 0.13.1 has no apply-flow gate / Agent._is_apply_flow_task — value-pattern
        # detection runs unconditionally; this task string is just for run stability.)
        task="Re-run the saved job application with substituted applicant data.",
        llm=_make_llm(args.model),
        tools=_gmail_tools(args.gmail_access_token, args.gmail_credentials, args.gmail_token),
        browser=_browser(args.headless),
        save_conversation_path=_trace_path(args, args.model),  # built-in per-step cause trace
        calculate_cost=True,
    )
    print(f"[replay] model={args.model} url-from-history={args.history}")
    # Always report the cache cost — even if the deterministic rerun stops early on a
    # moved element (e.g. ephemeral react-select option). The whole point is to MEASURE
    # the LLM cost of the deterministic replay, which holds for the steps that did run.
    rerun = "completed"
    try:
        await agent.load_and_rerun(args.history, variables=variables or None)
    except Exception as exc:
        rerun = f"stopped early ({type(exc).__name__}: {str(exc).splitlines()[0][:80]})"

    # load_and_rerun populates the same usage tracker
    usage = await agent.token_cost_service.get_usage_summary()
    print("\n----- REPLAY (browser-use deterministic cache) -----")
    print(f"  rerun: {rerun}")
    print(f"  COST:  ${usage.total_cost:.4f}   <-- LLM cost of replaying the cached script")
    print(f"  prompt {usage.total_prompt_tokens:,} (cached {usage.total_prompt_cached_tokens:,}) "
          f"| completion {usage.total_completion_tokens:,}")
    print("  (deterministic fills replay with NO LLM call; any cost is a live re-eval step)")
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
        sp.add_argument("--trace", default="runs/trace",
                        help="Dir for per-step cause trace (browser-use save_conversation_path). "
                             "Pass --trace '' to disable.")

    def job_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--job-url", required=True)
        sp.add_argument("--resume", default=None)
        sp.add_argument("--max-steps", type=int, default=80)
        sp.add_argument("--max-actions", type=int, default=3,
                        help="Actions per step. Default 3 (~$0.13, reliable now that the retry-then-vision "
                             "fix killed the re-fill loop: phone 3x, 4 nudges, done). 1 = most reliable but "
                             "~2x cost ($0.27, 64 steps). 2 is dominated — avoid.")

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
