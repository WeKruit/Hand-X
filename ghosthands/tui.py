"""Rich terminal UI for running Hand-X without the Electron desktop app."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text


class TuiEvent(BaseModel):
    """Validated JSONL event from the Hand-X engine process."""

    model_config = ConfigDict(extra="allow")

    event: str
    timestamp: int | float | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_event_name(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value.get("event") and value.get("type"):
            value = {**value, "event": value["type"]}
        return value

    @property
    def data(self) -> dict[str, Any]:
        return self.model_extra or {}


@dataclass
class TuiRunState:
    """Small render state derived from engine JSONL events."""

    job_url: str = ""
    phase: str = "Setup"
    status: str = "Waiting for engine"
    step: int | None = None
    max_steps: int | None = None
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fields_filled: int = 0
    fields_failed: int = 0
    cdp_url: str = ""
    page_url: str = ""
    user_id: str = ""
    job_id: str = ""
    lease_id: str = ""
    sync_status: str = "Local run"
    awaiting_review: bool = False
    review_ready: bool = False
    pending_question: dict[str, Any] | None = None
    done: bool = False
    success: bool | None = None
    message: str = ""
    process_returncode: int | None = None
    logs: list[tuple[str, str]] = field(default_factory=list)

    def add_log(self, kind: str, message: str) -> None:
        message = " ".join(str(message or "").split())
        if not message:
            return
        if len(message) > 180:
            message = f"{message[:177]}..."
        self.logs.append((kind, message))
        del self.logs[:-12]

    def apply_event(self, event: TuiEvent) -> None:
        kind = event.event
        data = event.data

        if kind == "handshake":
            self.status = "Engine connected"
            version = data.get("protocol_version")
            self.add_log("engine", f"Protocol v{version}" if version else "Protocol ready")
            return

        if kind == "phase":
            self.phase = str(data.get("phase") or self.phase)
            detail = data.get("detail")
            self.status = str(detail or self.phase)
            self.add_log("phase", self.status)
            return

        if kind == "status":
            self.status = str(data.get("message") or self.status)
            self.step = _maybe_int(data.get("step"), self.step)
            self.max_steps = _maybe_int(data.get("maxSteps"), self.max_steps)
            self.add_log("status", self.status)
            return

        if kind == "progress":
            self.step = _maybe_int(data.get("step"), self.step)
            self.max_steps = _maybe_int(data.get("maxSteps"), self.max_steps)
            description = str(data.get("description") or "Progress updated")
            self.status = description
            self.add_log("progress", description)
            return

        if kind == "field_filled":
            self.fields_filled += 1
            field_name = str(data.get("field") or "field")
            value = _safe_value(field_name, data.get("value"))
            self.add_log("filled", f"{field_name}: {value}")
            return

        if kind == "field_failed":
            self.fields_failed += 1
            field_name = str(data.get("field") or "field")
            reason = str(data.get("reason") or "failed")
            self.add_log("failed", f"{field_name}: {reason}")
            return

        if kind == "cost":
            self.cost_usd = float(data.get("total_usd") or 0.0)
            self.prompt_tokens = int(data.get("prompt_tokens") or 0)
            self.completion_tokens = int(data.get("completion_tokens") or 0)
            return

        if kind == "browser_ready":
            self.cdp_url = str(data.get("cdpUrl") or "")
            self.status = "Browser ready"
            self.add_log("browser", "Browser ready for automation")
            return

        if kind == "needs_answer":
            questions = data.get("questions")
            question = questions[0] if isinstance(questions, list) and questions else data.get("field")
            if isinstance(question, dict):
                self.pending_question = question
                self.phase = "Input required"
                label = str(question.get("fieldLabel") or question.get("label") or "Field input")
                self.status = str(data.get("message") or label)
                self.add_log("input", label)
            return

        if kind == "paused":
            self.status = str(data.get("message") or "Paused")
            self.add_log("control", self.status)
            return

        if kind == "resumed":
            self.status = str(data.get("message") or "Resumed")
            self.add_log("control", self.status)
            return

        if kind == "review_ready":
            self.review_ready = True
            self.phase = "Review"
            self.status = str(data.get("message") or "Application ready for review")
            self.cdp_url = str(data.get("cdpUrl") or self.cdp_url)
            self.page_url = str(data.get("pageUrl") or self.page_url)
            self.add_log("review", self.status)
            return

        if kind == "account_created":
            platform = str(data.get("platform") or "platform")
            email = str(data.get("email") or "account")
            status = str(data.get("credentialStatus") or "created")
            self.add_log("account", f"{platform}: {email} ({status})")
            return

        if kind == "awaiting_review":
            self.awaiting_review = True
            self.phase = "Review"
            self.status = str(data.get("message") or "Review the application in the browser")
            self.cdp_url = str(data.get("cdpUrl") or self.cdp_url)
            self.page_url = str(data.get("pageUrl") or self.page_url)
            self.add_log("review", self.status)
            return

        if kind == "error":
            self.status = str(data.get("message") or "Engine error")
            self.add_log("error", self.status)
            return

        if kind == "done":
            self.done = True
            self.success = bool(data.get("success"))
            self.message = str(data.get("message") or "")
            self.status = self.message or ("Done" if self.success else "Finished")
            self.fields_filled = _maybe_int(data.get("fields_filled"), self.fields_filled) or 0
            self.fields_failed = _maybe_int(data.get("fields_failed"), self.fields_failed) or 0
            self.add_log("done", self.status)
            return

        if kind == "lease_acquired":
            self.lease_id = str(data.get("leaseId") or self.lease_id)
            self.job_id = str(data.get("jobId") or self.job_id)
            self.sync_status = "VALET sync active"
            self.add_log("sync", f"Lease acquired: {self.lease_id or 'unknown'}")
            return

        if kind == "lease_heartbeat":
            self.lease_id = str(data.get("leaseId") or self.lease_id)
            self.sync_status = "VALET heartbeat"
            return

        if kind == "lease_released":
            self.lease_id = str(data.get("leaseId") or self.lease_id)
            reason = str(data.get("reason") or "completed")
            self.sync_status = f"VALET released ({reason})"
            self.add_log("sync", self.sync_status)
            return

        self.add_log(kind, kind)


def parse_jsonl_event(line: str) -> TuiEvent | None:
    """Parse one engine JSONL line into a validated event."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return TuiEvent.model_validate(payload)
    except ValueError:
        return None


def build_engine_argv(args: argparse.Namespace, executable: Sequence[str] | None = None) -> list[str]:
    """Build the JSONL child-process argv for the current TUI request."""
    if executable is not None:
        argv = list(executable)
    elif getattr(sys, "frozen", False):
        argv = [sys.executable]
    else:
        argv = [sys.executable, "-m", "ghosthands.cli"]

    def add_option(name: str, value: Any) -> None:
        if value is None or value == "":
            return
        argv.extend([name, str(value)])

    add_option("--job-url", args.job_url)
    add_option("--profile", args.profile)
    add_option("--test-data", args.test_data)
    add_option("--user-id", args.user_id)
    add_option("--resume-id", args.resume_id)
    add_option("--resume", args.resume)
    add_option("--job-id", args.job_id)
    add_option("--lease-id", args.lease_id)
    add_option("--model", args.model)
    add_option("--max-steps", args.max_steps)
    add_option("--max-budget", args.max_budget)
    add_option("--submit-intent", "review")
    add_option("--proxy-url", args.proxy_url)
    add_option("--runtime-grant", args.runtime_grant)
    add_option("--allowed-domains", args.allowed_domains)
    add_option("--browsers-path", args.browsers_path)
    add_option("--cdp-url", args.cdp_url)
    add_option("--cdp-target-id", getattr(args, "cdp_target_id", None))
    add_option("--engine", getattr(args, "engine", None))
    if args.headless:
        argv.append("--headless")
    argv.extend(["--output-format", "jsonl"])
    return argv


async def run_tui(args: argparse.Namespace) -> None:
    """Prompt for any missing inputs, run the JSONL engine, and render progress."""
    console = Console()
    args = _collect_tui_args(args, console)
    state = TuiRunState(
        job_url=args.job_url,
        max_steps=args.max_steps,
        user_id=str(args.user_id or ""),
        job_id=str(args.job_id or ""),
        lease_id=str(args.lease_id or ""),
        sync_status=_sync_status_label(args),
    )
    argv = build_engine_argv(args)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    console.print(_intro_panel(args))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stdout_task = asyncio.create_task(_read_stdout(proc, state))
    stderr_task = asyncio.create_task(_read_stderr(proc, state))
    review_command_sent = False

    try:
        while proc.returncode is None:
            with Live(_render_state(state), console=console, refresh_per_second=4) as live:
                while proc.returncode is None and not (
                    state.pending_question or (state.awaiting_review and not review_command_sent)
                ):
                    live.update(_render_state(state))
                    await asyncio.sleep(0.25)

            if state.pending_question:
                await _handle_hitl_prompt(proc, state, console)
                state.pending_question = None
                state.status = "Input sent; automation resuming"
                continue

            if state.awaiting_review and not review_command_sent:
                await _handle_review_prompt(proc, state, console)
                review_command_sent = True
                state.awaiting_review = False
                state.status = "Review command sent; waiting for engine shutdown"
                continue

        state.process_returncode = await proc.wait()
        await _finish_reader(stdout_task)
        await _finish_reader(stderr_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await _cancel_engine(proc, state, console)
        raise
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    console.print(_final_panel(state))
    if state.process_returncode not in (0, None) and not state.success:
        raise SystemExit(state.process_returncode)


def _collect_tui_args(args: argparse.Namespace, console: Console) -> argparse.Namespace:
    """Fill missing CLI args from terminal prompts."""
    values = vars(args).copy()
    missing_core = not values.get("job_url")
    has_profile_source = any(values.get(key) for key in ("profile", "test_data", "user_id")) or bool(
        os.getenv("GH_USER_PROFILE_PATH") or os.getenv("GH_USER_PROFILE_TEXT")
    )

    if (missing_core or not has_profile_source) and not sys.stdin.isatty():
        raise SystemExit("TUI mode needs an interactive terminal when job/profile inputs are missing.")

    values["submit_intent"] = "review"

    if not sys.stdin.isatty():
        values["output_format"] = "tui"
        return argparse.Namespace(**values)

    if not values.get("job_url"):
        values["job_url"] = Prompt.ask("Job URL").strip()
    if not values.get("job_url"):
        raise SystemExit("Job URL is required.")

    if not has_profile_source:
        source = Prompt.ask(
            "Profile source",
            choices=["profile", "test-data", "user-id", "env"],
            default="profile",
        )
        if source == "profile":
            values["profile"] = Prompt.ask("Profile JSON or @file").strip()
        elif source == "test-data":
            values["test_data"] = Prompt.ask("Test data JSON path").strip()
        elif source == "user-id":
            values["user_id"] = Prompt.ask("User ID").strip()
            resume_id = Prompt.ask("Resume ID (optional)", default="").strip()
            values["resume_id"] = resume_id or None

    if not values.get("resume"):
        resume = Prompt.ask("Resume PDF path (optional)", default="").strip()
        values["resume"] = resume or None

    if not values.get("model"):
        model = Prompt.ask("Model override (optional)", default="").strip()
        values["model"] = model or None

    values["max_steps"] = int(Prompt.ask("Max steps", default=str(values.get("max_steps") or 50)))
    values["max_budget"] = float(Prompt.ask("Max budget USD", default=str(values.get("max_budget") or 0.5)))
    values["headless"] = Confirm.ask("Run browser headless?", default=bool(values.get("headless")))

    _validate_path(values.get("resume"), "Resume")
    if values.get("profile") and str(values["profile"]).startswith("@"):
        _validate_path(str(values["profile"])[1:], "Profile")
    _validate_path(values.get("test_data"), "Test data")

    values["resume"] = _normalize_path(values.get("resume"))
    values["test_data"] = _normalize_path(values.get("test_data"))
    if values.get("profile") and str(values["profile"]).startswith("@"):
        values["profile"] = f"@{_normalize_path(str(values['profile'])[1:])}"

    values["output_format"] = "tui"
    return argparse.Namespace(**values)


def _intro_panel(args: argparse.Namespace) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Job", str(args.job_url))
    table.add_row("Profile", _profile_source_label(args))
    table.add_row("Resume", str(args.resume or "none"))
    table.add_row("Mode", "review")
    table.add_row("Browser", "headless" if args.headless else "visible")
    table.add_row("Sync", _sync_source_label(args))
    return Panel(table, title="Hand-X Terminal", border_style="cyan")


def _render_state(state: TuiRunState) -> Group:
    summary = Table.grid(expand=True)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        _metric("Phase", state.phase),
        _metric("Progress", _progress_label(state)),
        _metric("Cost", f"${state.cost_usd:.4f}"),
    )
    summary.add_row(
        _metric("Fields", f"{state.fields_filled} filled / {state.fields_failed} failed"),
        _metric("Tokens", f"{state.prompt_tokens} in / {state.completion_tokens} out"),
        _metric("Browser", "ready" if state.cdp_url else "starting"),
    )
    summary.add_row(
        _metric("Sync", state.sync_status),
        _metric("Job", state.job_id or "local"),
        _metric("Lease", state.lease_id or "-"),
    )

    log_table = Table(expand=True, show_header=True, header_style="bold cyan")
    log_table.add_column("Type", no_wrap=True, width=10)
    log_table.add_column("Latest activity")
    for kind, message in state.logs[-12:]:
        log_table.add_row(kind, Text(message))
    if not state.logs:
        log_table.add_row("setup", "Waiting for engine output")

    status = Text(state.status or "", style="bold")
    return Group(
        Panel(summary, title="Run", border_style="cyan"),
        Panel(status, title="Status", border_style="green" if not state.done else "cyan"),
        Panel(log_table, title="Events", border_style="blue"),
    )


async def _read_stdout(proc: asyncio.subprocess.Process, state: TuiRunState) -> None:
    if proc.stdout is None:
        return
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        event = parse_jsonl_event(line)
        if event is None:
            state.add_log("stdout", line)
            continue
        state.apply_event(event)


async def _read_stderr(proc: asyncio.subprocess.Process, state: TuiRunState) -> None:
    if proc.stderr is None:
        return
    async for raw in proc.stderr:
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            state.add_log("log", line)


async def _handle_review_prompt(
    proc: asyncio.subprocess.Process,
    state: TuiRunState,
    console: Console,
) -> None:
    console.print(_review_panel(state))
    complete = Confirm.ask("Mark review complete and detach the engine?", default=False)
    command = {"type": "complete_review"} if complete else {"type": "cancel_job"}
    await _send_command(proc, command)


async def _handle_hitl_prompt(
    proc: asyncio.subprocess.Process,
    state: TuiRunState,
    console: Console,
) -> None:
    question = state.pending_question or {}
    field_id = str(question.get("fieldId") or question.get("id") or "")
    field_label = str(question.get("fieldLabel") or question.get("label") or field_id)
    options = question.get("options")
    console.print(f"\n[bold]{field_label}[/bold]")
    if isinstance(options, list) and options:
        skip_choice = "<skip>"
        choices = [str(option) for option in options]
        selected = Prompt.ask("Answer", choices=[*choices, skip_choice], default=choices[0])
        answer = "" if selected == skip_choice else selected.strip()
    else:
        answer = Prompt.ask("Answer (leave blank to skip)", default="").strip()
    if answer:
        await _send_command(
            proc,
            {
                "type": "answer_field",
                "field_id": field_id,
                "field_label": field_label,
                "answer": answer,
            },
        )
    else:
        await _send_command(
            proc,
            {"type": "skip_field", "field_id": field_id, "field_label": field_label},
        )


async def _send_command(proc: asyncio.subprocess.Process, command: dict[str, Any]) -> None:
    if proc.stdin is None:
        return
    proc.stdin.write((json.dumps(command, separators=(",", ":")) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _cancel_engine(proc: asyncio.subprocess.Process, state: TuiRunState, console: Console) -> None:
    state.status = "Cancelling"
    console.print("\nCancelling Hand-X...")
    with contextlib.suppress(Exception):
        await _send_command(proc, {"type": "cancel_job"})
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)


async def _finish_reader(task: asyncio.Task[None]) -> None:
    if task.done():
        await task
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _review_panel(state: TuiRunState) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Status", state.status)
    if state.page_url:
        table.add_row("Page", state.page_url)
    if state.cdp_url:
        table.add_row("CDP", state.cdp_url)
    table.add_row("Next", "Review the visible browser window before confirming.")
    return Panel(table, title="Review Required", border_style="yellow")


def _final_panel(state: TuiRunState) -> Panel:
    ok = state.success is True
    title = "Complete" if ok else "Stopped"
    style = "green" if ok else "red"
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Result", state.message or state.status)
    table.add_row("Fields", f"{state.fields_filled} filled / {state.fields_failed} failed")
    table.add_row("Cost", f"${state.cost_usd:.4f}")
    if state.process_returncode is not None:
        table.add_row("Exit", str(state.process_returncode))
    return Panel(table, title=title, border_style=style)


def _metric(label: str, value: str) -> Panel:
    body = Text(str(value), style="bold")
    body.append(f"\n{label}", style="dim")
    return Panel(body, padding=(0, 1))


def _progress_label(state: TuiRunState) -> str:
    if state.step is None:
        return f"0/{state.max_steps}" if state.max_steps else "-"
    if state.max_steps:
        return f"{state.step}/{state.max_steps}"
    return str(state.step)


def _safe_value(field_name: str, value: Any) -> str:
    name = str(field_name or "").lower()
    if any(token in name for token in ("password", "secret", "token", "credential")):
        return "[redacted]"
    text = " ".join(str(value or "").split())
    if not text:
        return "(set)"
    if len(text) > 80:
        return f"{text[:77]}..."
    return text


def _maybe_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    with contextlib.suppress(TypeError, ValueError):
        return int(value)
    return default


def _profile_source_label(args: argparse.Namespace) -> str:
    if args.profile:
        return str(args.profile)
    if args.test_data:
        return str(args.test_data)
    if args.user_id:
        return f"user-id:{args.user_id}"
    if os.getenv("GH_USER_PROFILE_PATH"):
        return f"env:{os.getenv('GH_USER_PROFILE_PATH')}"
    if os.getenv("GH_USER_PROFILE_TEXT"):
        return "env:GH_USER_PROFILE_TEXT"
    return "none"


def _sync_status_label(args: argparse.Namespace) -> str:
    if args.job_id or args.lease_id or args.user_id or args.proxy_url:
        return "VALET sync pending"
    return "Local run"


def _sync_source_label(args: argparse.Namespace) -> str:
    parts = []
    if args.user_id:
        parts.append(f"user:{args.user_id}")
    if args.job_id:
        parts.append(f"job:{args.job_id}")
    if args.lease_id:
        parts.append(f"lease:{args.lease_id}")
    if args.proxy_url:
        parts.append("runtime:VALET")
    return ", ".join(parts) if parts else "local only"


def _validate_path(value: str | None, label: str) -> None:
    if not value:
        return
    path = Path(value).expanduser()
    if not path.exists():
        raise SystemExit(f"{label} path does not exist: {value}")


def _normalize_path(value: str | None) -> str | None:
    if not value:
        return None
    return str(Path(value).expanduser())
