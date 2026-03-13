"""Regression tests for ghosthands.cli argument parsing.

These tests capture the behavior of the CLI argument parser. S4 added
--allowed-domains for domain lockdown wiring.

Because ghosthands.cli imports heavy dependencies at module level
(ghosthands.agent.hooks -> browser-use -> cdp_use), we mock those modules
before importing the CLI module.  parse_args() reads from sys.argv, so we
patch sys.argv for each test.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module-level setup: mock heavy imports before loading ghosthands.cli
# ---------------------------------------------------------------------------

def _ensure_cli_importable():
    """Install lightweight mocks for browser-use dependency chain.

    ghosthands.cli imports ghosthands.agent.hooks at module level, which
    triggers browser_use -> cdp_use.  We intercept that chain with empty
    module stubs so parse_args() can be tested without a full environment.

    This is idempotent -- safe to call multiple times.
    """
    stubs = {
        "cdp_use": types.ModuleType("cdp_use"),
        "browser_use": types.ModuleType("browser_use"),
        "browser_use.agent": types.ModuleType("browser_use.agent"),
        "browser_use.agent.service": types.ModuleType("browser_use.agent.service"),
        "browser_use.browser": types.ModuleType("browser_use.browser"),
        "browser_use.browser.session": types.ModuleType("browser_use.browser.session"),
        "browser_use.tools": types.ModuleType("browser_use.tools"),
        "browser_use.tools.service": types.ModuleType("browser_use.tools.service"),
    }
    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)

    # Mock ghosthands.agent and its submodules to prevent cascading imports
    if "ghosthands.agent" not in sys.modules:
        mock_agent = types.ModuleType("ghosthands.agent")
        sys.modules["ghosthands.agent"] = mock_agent
    if "ghosthands.agent.factory" not in sys.modules:
        mock_factory = types.ModuleType("ghosthands.agent.factory")
        sys.modules["ghosthands.agent.factory"] = mock_factory
    if "ghosthands.agent.hooks" not in sys.modules:
        mock_hooks = types.ModuleType("ghosthands.agent.hooks")
        mock_hooks.install_same_tab_guard = AsyncMock()
        sys.modules["ghosthands.agent.hooks"] = mock_hooks


_ensure_cli_importable()

from ghosthands.cli import parse_args  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(argv: list[str]) -> object:
    """Run parse_args() with a synthetic sys.argv."""
    with patch.object(sys, "argv", ["hand-x"] + argv):
        return parse_args()


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestParseArgsDefaults:
    """Verify default values for all CLI arguments."""

    def test_job_url_is_required(self):
        """--job-url is required; omitting it causes SystemExit."""
        with pytest.raises(SystemExit):
            _parse([])

    def test_defaults_with_required_args(self):
        """All optional args have expected defaults when only --job-url is given."""
        args = _parse(["--job-url", "https://example.com/job"])

        assert args.job_url == "https://example.com/job"
        assert args.profile is None
        assert args.test_data is None
        assert args.resume is None
        assert args.job_id == ""
        assert args.lease_id == ""
        assert args.model is None
        assert args.max_steps == 50
        assert args.max_budget == 0.50
        assert args.headless is False
        assert args.email is None
        assert args.password is None
        assert args.output_format == "jsonl"
        assert args.proxy_url is None
        assert args.runtime_grant is None
        assert args.browsers_path is None


# ---------------------------------------------------------------------------
# --job-url
# ---------------------------------------------------------------------------


class TestJobUrl:
    """Tests for the --job-url argument."""

    def test_job_url_parsed(self):
        """--job-url value is stored as job_url."""
        args = _parse(["--job-url", "https://boards.greenhouse.io/acme/jobs/123"])
        assert args.job_url == "https://boards.greenhouse.io/acme/jobs/123"

    def test_job_url_with_query_params(self):
        """URLs with query parameters are preserved as-is."""
        url = "https://example.com/apply?ref=abc&source=linkedin"
        args = _parse(["--job-url", url])
        assert args.job_url == url


# ---------------------------------------------------------------------------
# --profile and --test-data
# ---------------------------------------------------------------------------


class TestProfileArgs:
    """Tests for --profile and --test-data arguments."""

    def test_profile_inline_json(self):
        """--profile accepts an inline JSON string."""
        args = _parse([
            "--job-url", "https://example.com",
            "--profile", '{"name": "Jane Doe"}',
        ])
        assert args.profile == '{"name": "Jane Doe"}'

    def test_profile_at_filepath(self):
        """--profile accepts @filepath syntax (string is passed through as-is)."""
        args = _parse([
            "--job-url", "https://example.com",
            "--profile", "@/tmp/profile.json",
        ])
        assert args.profile == "@/tmp/profile.json"

    def test_test_data_path(self):
        """--test-data stores a file path string."""
        args = _parse([
            "--job-url", "https://example.com",
            "--test-data", "examples/sample.json",
        ])
        assert args.test_data == "examples/sample.json"

    def test_profile_default_none(self):
        """--profile defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.profile is None

    def test_test_data_default_none(self):
        """--test-data defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.test_data is None


# ---------------------------------------------------------------------------
# --headless
# ---------------------------------------------------------------------------


class TestHeadless:
    """Tests for the --headless flag."""

    def test_headless_default_false(self):
        """--headless defaults to False (store_true action)."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.headless is False

    def test_headless_when_set(self):
        """--headless sets headless to True."""
        args = _parse(["--job-url", "https://example.com", "--headless"])
        assert args.headless is True


# ---------------------------------------------------------------------------
# --max-steps and --max-budget
# ---------------------------------------------------------------------------


class TestNumericArgs:
    """Tests for --max-steps and --max-budget."""

    def test_max_steps_default(self):
        """--max-steps defaults to 50."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.max_steps == 50

    def test_max_steps_custom(self):
        """--max-steps accepts a custom integer."""
        args = _parse(["--job-url", "https://example.com", "--max-steps", "100"])
        assert args.max_steps == 100

    def test_max_steps_is_int(self):
        """--max-steps is parsed as an integer, not a string."""
        args = _parse(["--job-url", "https://example.com", "--max-steps", "25"])
        assert isinstance(args.max_steps, int)

    def test_max_budget_default(self):
        """--max-budget defaults to 0.50."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.max_budget == 0.50

    def test_max_budget_custom(self):
        """--max-budget accepts a custom float."""
        args = _parse(["--job-url", "https://example.com", "--max-budget", "1.25"])
        assert args.max_budget == 1.25

    def test_max_budget_is_float(self):
        """--max-budget is parsed as a float."""
        args = _parse(["--job-url", "https://example.com", "--max-budget", "0.75"])
        assert isinstance(args.max_budget, float)


# ---------------------------------------------------------------------------
# --output-format
# ---------------------------------------------------------------------------


class TestOutputFormat:
    """Tests for the --output-format argument."""

    def test_output_format_default_jsonl(self):
        """--output-format defaults to 'jsonl'."""
        args = _parse(["--job-url", "https://example.com"])
        # BASELINE: default is "jsonl"
        assert args.output_format == "jsonl"

    def test_output_format_human(self):
        """--output-format accepts 'human'."""
        args = _parse(["--job-url", "https://example.com", "--output-format", "human"])
        assert args.output_format == "human"

    def test_output_format_jsonl_explicit(self):
        """--output-format accepts explicit 'jsonl'."""
        args = _parse(["--job-url", "https://example.com", "--output-format", "jsonl"])
        assert args.output_format == "jsonl"

    def test_output_format_invalid_rejected(self):
        """Invalid --output-format values are rejected."""
        with pytest.raises(SystemExit):
            _parse(["--job-url", "https://example.com", "--output-format", "xml"])


# ---------------------------------------------------------------------------
# VALET proxy args
# ---------------------------------------------------------------------------


class TestProxyArgs:
    """Tests for --proxy-url and --runtime-grant."""

    def test_proxy_url_default_none(self):
        """--proxy-url defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.proxy_url is None

    def test_proxy_url_set(self):
        """--proxy-url stores the provided URL."""
        args = _parse([
            "--job-url", "https://example.com",
            "--proxy-url", "https://valet.example.com/api/v1/local-workers/anthropic",
        ])
        assert args.proxy_url == "https://valet.example.com/api/v1/local-workers/anthropic"

    def test_runtime_grant_default_none(self):
        """--runtime-grant defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.runtime_grant is None

    def test_runtime_grant_set(self):
        """--runtime-grant stores the provided token."""
        args = _parse([
            "--job-url", "https://example.com",
            "--runtime-grant", "lwrg_v1_abc123",
        ])
        assert args.runtime_grant == "lwrg_v1_abc123"


# ---------------------------------------------------------------------------
# Credential args
# ---------------------------------------------------------------------------


class TestCredentialArgs:
    """Tests for --email and --password."""

    def test_email_default_none(self):
        """--email defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.email is None

    def test_password_default_none(self):
        """--password defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.password is None

    def test_email_and_password_set(self):
        """--email and --password store provided values."""
        args = _parse([
            "--job-url", "https://example.com",
            "--email", "user@test.com",
            "--password", "s3cret",
        ])
        assert args.email == "user@test.com"
        assert args.password == "s3cret"


# ---------------------------------------------------------------------------
# --resume and --browsers-path
# ---------------------------------------------------------------------------


class TestPathArgs:
    """Tests for path-based arguments."""

    def test_resume_default_none(self):
        """--resume defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.resume is None

    def test_resume_set(self):
        """--resume stores the provided file path."""
        args = _parse([
            "--job-url", "https://example.com",
            "--resume", "/tmp/resume.pdf",
        ])
        assert args.resume == "/tmp/resume.pdf"

    def test_browsers_path_default_none(self):
        """--browsers-path defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.browsers_path is None

    def test_browsers_path_set(self):
        """--browsers-path stores the provided path."""
        args = _parse([
            "--job-url", "https://example.com",
            "--browsers-path", "/opt/playwright/browsers",
        ])
        assert args.browsers_path == "/opt/playwright/browsers"


# ---------------------------------------------------------------------------
# --model and --job-id / --lease-id
# ---------------------------------------------------------------------------


class TestMiscArgs:
    """Tests for --model, --job-id, and --lease-id."""

    def test_model_default_none(self):
        """--model defaults to None."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.model is None

    def test_model_set(self):
        """--model stores the provided model name."""
        args = _parse([
            "--job-url", "https://example.com",
            "--model", "claude-sonnet-4-20250514",
        ])
        assert args.model == "claude-sonnet-4-20250514"

    def test_job_id_default_empty(self):
        """--job-id defaults to empty string."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.job_id == ""

    def test_job_id_set(self):
        """--job-id stores the provided value."""
        args = _parse([
            "--job-url", "https://example.com",
            "--job-id", "job-abc-123",
        ])
        assert args.job_id == "job-abc-123"

    def test_lease_id_default_empty(self):
        """--lease-id defaults to empty string."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.lease_id == ""

    def test_lease_id_set(self):
        """--lease-id stores the provided value."""
        args = _parse([
            "--job-url", "https://example.com",
            "--lease-id", "lease-xyz",
        ])
        assert args.lease_id == "lease-xyz"


# ---------------------------------------------------------------------------
# Backwards-compatible "apply" subcommand stripping
# ---------------------------------------------------------------------------


class TestApplySubcommand:
    """Tests for the backwards-compatible 'apply' subcommand stripping."""

    def test_apply_subcommand_stripped(self):
        """'apply' as the first positional arg is silently stripped."""
        args = _parse(["apply", "--job-url", "https://example.com"])
        assert args.job_url == "https://example.com"

    def test_without_apply_subcommand(self):
        """Parser works normally without the 'apply' prefix."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.job_url == "https://example.com"


# ---------------------------------------------------------------------------
# Documenting gaps for future streams
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# --allowed-domains (S4: domain lockdown wiring)
# ---------------------------------------------------------------------------


class TestAllowedDomains:
    """Tests for --allowed-domains argument (added in S4)."""

    def test_allowed_domains_default_none(self):
        """--allowed-domains defaults to None when not provided."""
        args = _parse(["--job-url", "https://example.com"])
        assert args.allowed_domains is None

    def test_allowed_domains_single(self):
        """--allowed-domains accepts a single domain."""
        args = _parse([
            "--job-url", "https://example.com",
            "--allowed-domains", "greenhouse.io",
        ])
        assert args.allowed_domains == "greenhouse.io"

    def test_allowed_domains_comma_separated(self):
        """--allowed-domains accepts a comma-separated list."""
        args = _parse([
            "--job-url", "https://example.com",
            "--allowed-domains", "greenhouse.io,lever.co,sso.company.com",
        ])
        assert args.allowed_domains == "greenhouse.io,lever.co,sso.company.com"

    def test_allowed_domains_is_string(self):
        """--allowed-domains is stored as a raw string (parsed later in CLI flow)."""
        args = _parse([
            "--job-url", "https://example.com",
            "--allowed-domains", "example.com",
        ])
        assert isinstance(args.allowed_domains, str)


# ---------------------------------------------------------------------------
# Remaining gaps for future streams
# ---------------------------------------------------------------------------


class TestFutureGaps:
    """Document arguments that do NOT currently exist."""

    def test_keep_alive_does_not_exist_as_cli_flag(self):
        """--keep-alive is NOT a current CLI argument.

        Note: keep_alive is hardcoded to True in the BrowserProfile construction
        inside run_agent_jsonl/run_agent_human, but is not exposed as a CLI flag.
        """
        with pytest.raises(SystemExit):
            _parse([
                "--job-url", "https://example.com",
                "--keep-alive",
            ])
