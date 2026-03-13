"""Integration tests — real subprocess spawning of the Hand-X CLI.

Verifies the JSONL protocol works end-to-end by spawning
``python -m ghosthands.cli`` as a real subprocess and inspecting
stdout, stderr, and exit codes.  No mocks, no monkeypatching.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Base env: inherit current env but force headless and suppress telemetry
_BASE_ENV = {
    **os.environ,
    "GH_HEADLESS": "true",
    "PYTHONUNBUFFERED": "1",
    # Prevent browser-use telemetry noise
    "DO_NOT_TRACK": "1",
}


def _cli_args(*extra: str) -> list[str]:
    """Build the CLI command list for ``python -m ghosthands.cli``."""
    return [sys.executable, "-m", "ghosthands.cli", *extra]


def _parse_jsonl(text: str) -> list[dict]:
    """Parse newline-delimited JSON from stdout text.

    Silently skips non-JSON lines.  Use ``_parse_jsonl_strict`` when every
    non-empty stdout line *must* be valid JSON.
    """
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            events.append(json.loads(line))
    return events


def _parse_jsonl_strict(text: str) -> list[dict]:
    """Parse newline-delimited JSON from stdout text — strict mode.

    Raises ``ValueError`` if any non-empty line is not valid JSON.
    This catches corrupted stdout that the lenient ``_parse_jsonl`` would
    silently swallow.
    """
    events = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"stdout line {lineno} is not valid JSON: {line!r}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"stdout line {lineno} parsed as {type(parsed).__name__}, expected dict: {line!r}")
        events.append(parsed)
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCLISubprocess:
    """Tests that spawn Hand-X CLI as a real subprocess."""

    def test_cli_help_exits_cleanly(self):
        """Spawn with --help, verify exit code 0 and usage text."""
        proc = subprocess.run(
            _cli_args("--help"),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        assert proc.returncode == 0
        stdout_lower = proc.stdout.lower()
        assert "usage" in stdout_lower or "hand-x" in stdout_lower

    def test_cli_help_mentions_job_url(self):
        """The --help output must document the --job-url flag."""
        proc = subprocess.run(
            _cli_args("--help"),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        assert "--job-url" in proc.stdout

    def test_cli_missing_job_url_errors(self):
        """Spawn without --job-url, verify non-zero exit code."""
        proc = subprocess.run(
            _cli_args(),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        # argparse exits with code 2 for missing required args
        assert proc.returncode != 0

    def test_cli_missing_job_url_shows_usage(self):
        """Without --job-url the CLI should print usage/error to stderr."""
        proc = subprocess.run(
            _cli_args(),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        combined = proc.stdout + proc.stderr
        assert "--job-url" in combined

    def test_handshake_is_first_event(self):
        """With --output-format=jsonl, the very first stdout line must be a handshake event.

        Uses strict JSONL parsing to ensure every stdout line is valid JSON
        (catches corrupted stdout that lenient parsing would silently skip).
        """
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        # The process will fail (no profile) but should still emit handshake first.
        # Strict parsing: fail if ANY non-empty stdout line is not valid JSON.
        events = _parse_jsonl_strict(proc.stdout)
        assert len(events) >= 1, f"Expected at least 1 JSONL event, got stdout: {proc.stdout!r}"
        assert events[0]["event"] == "handshake"
        assert events[0]["protocol_version"] == 1

    def test_handshake_includes_min_desktop_version(self):
        """Handshake event must include min_desktop_version field."""
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        events = _parse_jsonl(proc.stdout)
        assert len(events) >= 1
        handshake = events[0]
        assert "min_desktop_version" in handshake
        assert isinstance(handshake["min_desktop_version"], str)

    def test_missing_profile_emits_error_event(self):
        """Without --profile or --test-data, the CLI should emit an error event after handshake."""
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        events = _parse_jsonl(proc.stdout)
        assert len(events) >= 2, f"Expected handshake + error, got: {events}"
        error_event = events[1]
        assert error_event["event"] == "error"
        assert error_event["fatal"] is True
        assert "profile" in error_event["message"].lower()

    def test_all_events_have_timestamps(self):
        """Every JSONL event must have a numeric timestamp field."""
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        events = _parse_jsonl(proc.stdout)
        assert len(events) >= 1
        for event in events:
            assert "timestamp" in event, f"Missing timestamp in event: {event}"
            assert isinstance(event["timestamp"], int | float)

    def test_all_events_are_valid_json(self):
        """Every non-empty line on stdout in JSONL mode must be valid JSON.

        Uses ``_parse_jsonl_strict`` to fail on any non-JSON line rather than
        silently skipping it (which would mask corrupted stdout).
        """
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        events = _parse_jsonl_strict(proc.stdout)
        assert len(events) >= 1, f"Expected at least 1 JSONL event, got stdout: {proc.stdout!r}"

    def test_credential_env_vars_received(self):
        """Verify GH_EMAIL and GH_PASSWORD env vars are forwarded to subprocess."""
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('GH_EMAIL', 'MISSING')); "
                "print(os.environ.get('GH_PASSWORD', 'MISSING'))",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **_BASE_ENV,
                "GH_EMAIL": "bridge-test@example.com",
                "GH_PASSWORD": "bridge-secret-123",
            },
        )
        assert proc.returncode == 0
        assert "bridge-test@example.com" in proc.stdout
        assert "bridge-secret-123" in proc.stdout

    def test_cancel_stdin_write_does_not_hang(self):
        """Write a cancel command to stdin and verify the process exits without hanging.

        NOTE: This test does NOT verify that ``listen_for_cancel()`` actually
        processes the cancel command.  The CLI crashes before reaching the
        cancel listener because no valid profile is provided (exit code 1).
        The cancel command is sent to a dead/dying process.

        What this test *does* verify:
        1. Writing ``{"type":"cancel"}`` to stdin doesn't cause a deadlock/hang.
        2. The process exits within 15 seconds (no zombie).
        3. stdin pipe handling is robust (no broken pipe crash in the test).

        For actual cancel protocol testing, see
        ``test_bridge_protocol_real.py::TestListenForCancelReal`` which
        exercises ``listen_for_cancel()`` directly with real OS pipes.
        """
        proc = subprocess.Popen(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            env=_BASE_ENV,
        )
        try:
            # The CLI will fail fast due to missing profile, so we send cancel
            # right away; the process may already be exiting, which is fine.
            time.sleep(0.5)
            try:
                proc.stdin.write(b'{"type":"cancel"}\n')
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # Process may have already exited

            # Wait for exit with timeout
            proc.wait(timeout=15)
            # Process should have exited (any exit code is fine -- the
            # important thing is that it didn't hang)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_invalid_stdin_does_not_crash(self):
        """Send garbage to stdin; the process should not crash due to bad input."""
        proc = subprocess.Popen(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            env=_BASE_ENV,
        )
        try:
            time.sleep(0.3)
            try:
                proc.stdin.write(b"not valid json\n")
                proc.stdin.write(b"{malformed\n")
                proc.stdin.write(b"\x00\xff\xfe garbage bytes\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # Process may have already exited

            proc.wait(timeout=15)

            # Verify it exited (due to missing profile error, not a crash)
            stdout_text = proc.stdout.read().decode("utf-8", errors="replace")
            events = _parse_jsonl(stdout_text)
            # Should still have produced at least the handshake
            event_types = [e["event"] for e in events]
            assert "handshake" in event_types
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_output_format_jsonl_is_default(self):
        """When --output-format is not specified, jsonl is the default."""
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        # Default is jsonl, so stdout should contain the handshake event
        events = _parse_jsonl(proc.stdout)
        assert len(events) >= 1
        assert events[0]["event"] == "handshake"

    def test_human_format_no_jsonl_on_stdout(self):
        """With --output-format=human, stdout should NOT contain JSONL handshake."""
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "human",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        # In human mode, there should be no handshake event in JSONL format
        events = _parse_jsonl(proc.stdout)
        handshakes = [e for e in events if e.get("event") == "handshake"]
        assert len(handshakes) == 0

    def test_stderr_does_not_contain_jsonl(self):
        """In JSONL mode, stderr should NOT contain JSONL events (only logging)."""
        proc = subprocess.run(
            _cli_args(
                "--job-url",
                "https://example.com/job/12345",
                "--output-format",
                "jsonl",
                "--headless",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_BASE_ENV,
        )
        # stderr lines should not be parseable as JSONL events with "event" key
        for line in proc.stderr.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                # If it parsed as JSON, it should NOT be a JSONL protocol event
                assert "event" not in parsed or parsed.get("event") not in {
                    "handshake",
                    "status",
                    "error",
                    "done",
                    "progress",
                    "field_filled",
                    "field_failed",
                    "cost",
                    "browser_ready",
                    "phase",
                    "awaiting_review",
                    "account_created",
                }, f"JSONL event leaked to stderr: {parsed}"
            except json.JSONDecodeError:
                pass  # Expected -- stderr is logging text, not JSON
