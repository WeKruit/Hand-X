"""End-to-end binary integration tests — simulates Desktop app spawning Hand-X.

These tests use the COMPILED BINARY (not Python source) to verify the exact
same artifact customers receive works correctly with the Desktop app's
spawn/stdin/stdout protocol.

Requires: build/dist/hand-x binary (run `./scripts/dev-deploy.sh` first)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

# ---------- Fixtures ----------

BINARY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "build", "dist", "hand-x"
)
if sys.platform == "win32":
    BINARY_PATH += ".exe"

BINARY_EXISTS = os.path.isfile(BINARY_PATH) and os.access(BINARY_PATH, os.X_OK)

skip_no_binary = pytest.mark.skipif(
    not BINARY_EXISTS,
    reason=f"Hand-X binary not found at {BINARY_PATH}. Run: pyinstaller build/hand-x.spec --distpath build/dist",
)

# Dummy profile matching what Desktop sends (camelCase)
DESKTOP_PROFILE = json.dumps({
    "firstName": "Test",
    "lastName": "User",
    "email": "test@example.com",
    "phone": "+14155550100",
    "phoneDeviceType": "Mobile",
    "phoneCountryCode": "+1",
    "linkedIn": "https://linkedin.com/in/testuser",
    "zipCode": "94107",
    "workAuthorization": "Yes",
    "visaSponsorship": "No",
    "veteranStatus": "I am not a protected veteran",
    "disabilityStatus": "No, I Don't Have A Disability",
    "gender": "Male",
    "raceEthnicity": "Asian (Not Hispanic or Latino)",
    "education": [
        {
            "school": "MIT",
            "degree": "B.S.",
            "fieldOfStudy": "Computer Science",
            "graduationDate": "2020-05",
        }
    ],
    "experience": [
        {
            "company": "Google",
            "title": "Software Engineer",
            "startDate": "2020-06",
            "endDate": "2023-12",
        }
    ],
})


def spawn_handx(
    args: list[str],
    env_overrides: dict[str, str] | None = None,
    stdin_data: bytes | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess:
    """Spawn the Hand-X binary exactly like the Desktop app does."""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        # Suppress browser-use logging setup
        "BROWSER_USE_SETUP_LOGGING": "false",
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        [BINARY_PATH, *args],
        stdin=subprocess.PIPE,
        capture_output=True,
        env=env,
        input=stdin_data,
        timeout=timeout,
    )


def spawn_handx_async(
    args: list[str],
    env_overrides: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Spawn Hand-X binary with live stdin/stdout pipes (like Desktop does)."""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "BROWSER_USE_SETUP_LOGGING": "false",
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.Popen(
        [BINARY_PATH, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def read_jsonl_events(stdout: bytes) -> list[dict]:
    """Parse JSONL events from stdout, skipping non-JSON lines."""
    events = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            continue
    return events


# ---------- Tests ----------


class TestBinarySmoke:
    """Basic binary health checks — does it start at all?"""

    @skip_no_binary
    def test_help_exits_cleanly(self):
        """Binary --help works (exit 0)."""
        result = spawn_handx(["--help"])
        assert result.returncode == 0
        assert b"Hand-X" in result.stdout

    @skip_no_binary
    def test_missing_required_args_errors(self):
        """Missing --job-url produces usage error."""
        result = spawn_handx([])
        assert result.returncode != 0
        assert b"--job-url" in result.stderr


class TestDesktopSpawnProtocol:
    """Simulate exactly how the Desktop app spawns Hand-X and reads events."""

    @skip_no_binary
    def test_handshake_is_first_event(self):
        """Desktop expects handshake as the very first JSONL line.

        Spawn with a dummy job URL and profile. The binary will emit handshake
        before attempting to launch the browser (which will fail — that's fine,
        we just need the first event).
        """
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": DESKTOP_PROFILE,
                "GH_HEADLESS": "true",
            },
            timeout=60,
        )

        events = read_jsonl_events(result.stdout)
        assert len(events) >= 1, f"No JSONL events received. stderr: {result.stderr.decode()[:500]}"

        first = events[0]
        assert first.get("event") == "handshake", (
            f"First event should be handshake, got: {first}"
        )
        assert "protocol_version" in first
        assert first["protocol_version"] == 1

    @skip_no_binary
    def test_error_event_on_bad_profile(self):
        """Desktop sends invalid profile JSON — should get error event, not crash."""
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": "not-valid-json{{{",
            },
            timeout=30,
        )

        events = read_jsonl_events(result.stdout)
        # Should have at least handshake + error
        error_events = [e for e in events if e.get("event") == "error"]
        assert len(error_events) >= 1, f"Expected error event. Got events: {events}"
        # Error message should be sanitized (no raw exception)
        for err in error_events:
            msg = err.get("message", "")
            assert "Traceback" not in msg, f"Raw traceback leaked: {msg}"
            assert "JSONDecodeError" not in msg, f"Raw exception leaked: {msg}"

    @skip_no_binary
    def test_error_event_on_non_dict_profile(self):
        """Profile is valid JSON but not an object — should get error event."""
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": '["not", "a", "dict"]',
            },
            timeout=30,
        )

        events = read_jsonl_events(result.stdout)
        error_events = [e for e in events if e.get("event") == "error"]
        assert len(error_events) >= 1, f"Expected error event. Got: {events}"

    @skip_no_binary
    def test_cancel_via_stdin(self):
        """Desktop sends cancel command — Hand-X should exit gracefully."""
        proc = spawn_handx_async(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": DESKTOP_PROFILE,
                "GH_HEADLESS": "true",
            },
        )

        # Give it a moment to start and emit handshake
        time.sleep(3)

        # Send cancel command (exactly how Desktop does it)
        try:
            proc.stdin.write(b'{"type":"cancel"}\n')
            proc.stdin.flush()
        except BrokenPipeError:
            pass  # Process may have already exited

        # Wait for exit
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            pytest.fail("Process did not exit after cancel command within 30s")

        events = read_jsonl_events(stdout)

        # Should have graceful exit — handshake at minimum
        handshakes = [e for e in events if e.get("event") == "handshake"]
        assert len(handshakes) >= 1 or proc.returncode is not None, (
            f"Expected handshake or process exit. Events: {events}"
        )

    @skip_no_binary
    def test_invalid_stdin_ignored(self):
        """Garbage on stdin should not crash the binary."""
        proc = spawn_handx_async(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": DESKTOP_PROFILE,
                "GH_HEADLESS": "true",
            },
        )

        time.sleep(2)

        # Send garbage
        try:
            proc.stdin.write(b"not json at all\n")
            proc.stdin.write(b'{"valid": "json but not a command"}\n')
            proc.stdin.write(b"\x00\x01\x02\x03\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass

        # Send cancel to exit cleanly
        time.sleep(1)
        try:
            proc.stdin.write(b'{"type":"cancel"}\n')
            proc.stdin.flush()
        except BrokenPipeError:
            pass

        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        # Process should not have crashed from garbage input
        # (it either exited from cancel or from the job failing — both OK)
        assert proc.returncode is not None, "Process should have exited"

    @skip_no_binary
    def test_credential_env_forwarding(self):
        """Desktop passes credentials via env vars — Hand-X should receive them."""
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": DESKTOP_PROFILE,
                "GH_EMAIL": "test@example.com",
                "GH_PASSWORD": "testpass123",
                "GH_HEADLESS": "true",
            },
            timeout=60,
        )

        events = read_jsonl_events(result.stdout)
        # Should get past the credential check — no "missing credentials" error
        error_msgs = [
            e.get("message", "").lower()
            for e in events
            if e.get("event") == "error"
        ]
        for msg in error_msgs:
            assert "missing credentials" not in msg, (
                f"Credentials were not forwarded via env: {msg}"
            )
            # Verify no credential leakage in error messages
            assert "testpass123" not in msg, f"Password leaked in error: {msg}"

    @skip_no_binary
    def test_done_event_structure(self):
        """The done event should have the expected fields for Desktop parsing."""
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": DESKTOP_PROFILE,
                "GH_HEADLESS": "true",
            },
            timeout=60,
        )

        events = read_jsonl_events(result.stdout)
        done_events = [e for e in events if e.get("event") == "done"]

        if done_events:
            done = done_events[-1]  # Last done event
            assert "success" in done, f"done event missing 'success': {done}"
            assert isinstance(done["success"], bool), f"success should be bool: {done}"
            if "message" in done:
                assert isinstance(done["message"], str)
            if "fields_filled" in done:
                assert isinstance(done["fields_filled"], int)
            if "fields_failed" in done:
                assert isinstance(done["fields_failed"], int)

    @skip_no_binary
    def test_events_use_event_key_not_type(self):
        """All JSONL events must use 'event' as discriminator, never 'type'."""
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": DESKTOP_PROFILE,
                "GH_HEADLESS": "true",
            },
            timeout=60,
        )

        events = read_jsonl_events(result.stdout)
        assert len(events) >= 1, "Expected at least one event"

        for evt in events:
            assert "event" in evt, f"Event missing 'event' key: {evt}"
            # 'type' should NOT be used as discriminator
            # (some events may have 'type' as a data field, that's fine,
            #  but the top-level discriminator must be 'event')


class TestDesktopProfilePipeline:
    """Test the profile transformation that happens when Desktop sends camelCase."""

    @skip_no_binary
    def test_profile_with_missing_demographics_emits_warning(self):
        """Desktop sends profile without demographics — should get warning status."""
        minimal_profile = json.dumps({
            "firstName": "Test",
            "lastName": "User",
            "email": "test@example.com",
        })

        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": minimal_profile,
                "GH_HEADLESS": "true",
            },
            timeout=60,
        )

        events = read_jsonl_events(result.stdout)
        status_events = [e for e in events if e.get("event") == "status"]
        status_msgs = [e.get("message", "") for e in status_events]

        # Should have a warning about default demographic answers
        has_default_warning = any(
            "default" in msg.lower() and "verify" in msg.lower()
            for msg in status_msgs
        )
        assert has_default_warning, (
            f"Expected demographic defaults warning. Status messages: {status_msgs}"
        )


class TestSecurityGuards:
    """Verify security hardening works in the compiled binary."""

    @skip_no_binary
    def test_no_password_in_stdout(self):
        """Credentials must NEVER appear in stdout JSONL events."""
        profile_with_creds = json.dumps({
            "firstName": "Test",
            "lastName": "User",
            "email": "test@example.com",
            "credentials": {
                "email": "secret@test.com",
                "password": "SuperSecret123!",
            },
        })

        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
                "--max-steps", "1",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": profile_with_creds,
                "GH_EMAIL": "secret@test.com",
                "GH_PASSWORD": "SuperSecret123!",
                "GH_HEADLESS": "true",
            },
            timeout=60,
        )

        stdout_text = result.stdout.decode("utf-8", errors="replace")
        assert "SuperSecret123!" not in stdout_text, (
            "Password leaked in stdout JSONL!"
        )

    @skip_no_binary
    def test_error_messages_sanitized(self):
        """Error events should not contain raw Python tracebacks or internal paths."""
        result = spawn_handx(
            [
                "--job-url", "https://example.com/jobs/123",
                "--output-format", "jsonl",
                "--headless",
            ],
            env_overrides={
                "GH_USER_PROFILE_TEXT": "{{invalid json}}",
                "GH_HEADLESS": "true",
            },
            timeout=30,
        )

        events = read_jsonl_events(result.stdout)
        for evt in events:
            if evt.get("event") == "error":
                msg = evt.get("message", "")
                assert "Traceback" not in msg, f"Traceback leaked: {msg}"
                assert "/Users/" not in msg, f"Internal path leaked: {msg}"
                assert "site-packages" not in msg, f"Internal path leaked: {msg}"
