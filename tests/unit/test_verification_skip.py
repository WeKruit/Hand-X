"""Unit tests for verification-code blocker classification.

When an application hits an email/SMS verification wall, the run must
terminate with status "verification_code_required" and a user-facing
"skipped for now" message instead of the generic "agent_incomplete".

All tests are offline (no browser, no database, no API calls).
"""

from __future__ import annotations

import pytest

from ghosthands.cli import _is_verification_code_blocker


class TestIsVerificationCodeBlocker:
    @pytest.mark.parametrize(
        "blocker",
        [
            # Exact phrasing the agent prompt rule instructs (prompts.py _verification_rule)
            "blocker: email verification required — user must verify email then retry",
            "blocker: the page asks for a verification code sent to the inbox",
            "blocker: verification email sent, cannot proceed",
            "blocker: please verify your account before continuing",
            "blocker: verify your email address to continue",
            "blocker: a one-time code was sent to the user's email",
            "blocker: one time passcode required",
            "blocker: OTP required to sign in",
        ],
    )
    def test_matches_verification_walls(self, blocker: str) -> None:
        assert _is_verification_code_blocker(blocker) is True

    @pytest.mark.parametrize(
        "blocker",
        [
            None,
            "",
            "blocker: sign-in failed — invalid password",
            "blocker: captcha detected",
            "blocker: position closed",
            "blocker: required field 'Phone' could not be filled",
            # 'otp' must match as a whole word only
            "blocker: hotpot restaurant application form is broken",
        ],
    )
    def test_ignores_non_verification_blockers(self, blocker: str | None) -> None:
        assert _is_verification_code_blocker(blocker) is False
