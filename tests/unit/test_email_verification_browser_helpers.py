"""Unit tests for Phase 2 email-verification browser helpers."""

from __future__ import annotations

import json

import pytest

from ghosthands.email_verification import (
    CodeEntryStatus,
    EmailVerificationPageKind,
    EmailVerificationPageState,
    MagicLinkOpenStatus,
    classify_email_verification_page_state,
    extract_email_verification_page_state,
    fill_verification_code,
    is_auto_resolvable_email_page,
    open_magic_link_in_new_tab,
)


class FakePage:
    def __init__(self, raw_result):
        self.raw_result = raw_result
        self.evaluate_calls = []

    async def evaluate(self, script, *args):
        self.evaluate_calls.append((script, args))
        return self.raw_result


def _state(**overrides) -> EmailVerificationPageState:
    data = {
        "current_url": "https://jobs.example.com/verify-email",
        "site_hostname": "jobs.example.com",
        "visible_text": "Verify your email. We sent a verification code to candidate.qa@gmail.com.",
        "heading_text": "Verify your email",
        "application_email": "candidate.qa@gmail.com",
        "code_input_count": 1,
        "code_input_selectors": ["#verification-code"],
        "supports_code_entry": True,
        "email_signals": True,
    }
    data.update(overrides)
    state = EmailVerificationPageState(**data)
    return state.model_copy(update={"page_kind": classify_email_verification_page_state(state)})


def test_classifier_marks_email_code_pages_as_auto_resolvable() -> None:
    state = _state()

    assert state.page_kind is EmailVerificationPageKind.EMAIL_CODE
    assert is_auto_resolvable_email_page(state) is True


def test_classifier_marks_magic_link_pages_as_auto_resolvable() -> None:
    state = _state(
        visible_text="Check your email and click the verification link we sent to candidate.qa@gmail.com.",
        code_input_count=0,
        code_input_selectors=[],
        supports_code_entry=False,
        supports_magic_link=True,
        magic_link_signals=True,
    )

    assert state.page_kind is EmailVerificationPageKind.EMAIL_MAGIC_LINK
    assert is_auto_resolvable_email_page(state) is True


@pytest.mark.parametrize(
    ("text", "overrides", "expected"),
    [
        (
            "Enter the security code we sent by SMS to your phone.",
            {
                "heading_text": "Security verification",
                "sms_signals": True,
                "email_signals": False,
                "application_email": "",
            },
            EmailVerificationPageKind.SMS_CODE,
        ),
        (
            "Enter the six digit code from your authenticator app.",
            {
                "heading_text": "Security verification",
                "authenticator_signals": True,
                "email_signals": False,
                "application_email": "",
            },
            EmailVerificationPageKind.AUTHENTICATOR_CODE,
        ),
        (
            "Verify you are human before continuing. Complete the captcha.",
            {
                "heading_text": "Security verification",
                "captcha_signals": True,
                "email_signals": False,
                "application_email": "",
                "supports_code_entry": False,
            },
            EmailVerificationPageKind.CAPTCHA,
        ),
    ],
)
def test_classifier_rejects_non_email_recovery_pages(text, overrides, expected) -> None:
    state = _state(visible_text=text, **overrides)

    assert state.page_kind is expected
    assert is_auto_resolvable_email_page(state) is False


@pytest.mark.asyncio
async def test_extract_page_state_parses_json_string_and_classifies() -> None:
    page = FakePage(
        json.dumps(
            {
                "current_url": "https://jobs.example.com/verify-email",
                "site_hostname": "jobs.example.com",
                "visible_text": "Verify your email. We sent a code to candidate.qa@gmail.com.",
                "heading_text": "Email verification",
                "application_email": "candidate.qa@gmail.com",
                "code_input_count": 1,
                "code_input_selectors": ["#otp"],
                "supports_code_entry": True,
                "email_signals": True,
            }
        )
    )

    state = await extract_email_verification_page_state(page, platform="greenhouse", company_hint="Example")

    assert state.page_kind is EmailVerificationPageKind.EMAIL_CODE
    assert state.platform == "greenhouse"
    assert state.company_hint == "Example"
    assert state.code_input_selectors == ("#otp",)


@pytest.mark.asyncio
async def test_fill_verification_code_passes_state_selectors_and_parses_result() -> None:
    page = FakePage(
        {
            "status": "entered",
            "mode": "single_input",
            "filled_input_count": 1,
            "clicked_action": True,
            "clicked_action_label": "Verify",
            "page_url": "https://jobs.example.com/verify-email",
            "reason": "Verification code entered and a safe action button was clicked.",
        }
    )
    state = _state(code_input_selectors=["#otp-code"])

    result = await fill_verification_code(page, "482913", state=state)

    assert result.status is CodeEntryStatus.ENTERED
    assert result.success is True
    assert result.filled_input_count == 1
    assert result.clicked_action is True
    assert page.evaluate_calls[0][1][0] == {
        "code": "482913",
        "selectors": ["#otp-code"],
        "click_action": True,
    }


@pytest.mark.asyncio
async def test_fill_verification_code_rejects_empty_code_without_browser_call() -> None:
    page = FakePage({})

    result = await fill_verification_code(page, " ")

    assert result.status is CodeEntryStatus.MISSING_CODE
    assert result.success is False
    assert page.evaluate_calls == []


class FakeSwitchEvent:
    def __init__(self, target_id: str):
        self.target_id = target_id

    def __await__(self):
        async def _noop():
            return None

        return _noop().__await__()

    async def event_result(self, **_kwargs):
        return self.target_id


class FakeEventBus:
    def __init__(self, session):
        self.session = session
        self.switched_to = ""

    def dispatch(self, event):
        self.switched_to = event.target_id
        self.session.agent_focus_target_id = event.target_id
        self.session.current_url = self.session.original_url
        return FakeSwitchEvent(event.target_id)


class FakeBrowserSession:
    def __init__(self):
        self.original_url = "https://jobs.example.com/verify-email"
        self.current_url = self.original_url
        self.agent_focus_target_id = "original-target"
        self.event_bus = FakeEventBus(self)
        self.navigations = []

    async def get_current_page_url(self):
        return self.current_url

    async def navigate_to(self, url: str, new_tab: bool = False):
        self.navigations.append((url, new_tab))
        self.agent_focus_target_id = "magic-target"
        self.current_url = url


@pytest.mark.asyncio
async def test_open_magic_link_uses_new_tab_and_returns_to_original_target() -> None:
    session = FakeBrowserSession()

    result = await open_magic_link_in_new_tab(session, "https://jobs.example.com/verify?token=abc")

    assert result.status is MagicLinkOpenStatus.OPENED
    assert result.success is True
    assert result.original_target_id == "original-target"
    assert result.magic_link_target_id == "magic-target"
    assert result.returned_to_original is True
    assert session.navigations == [("https://jobs.example.com/verify?token=abc", True)]
    assert session.event_bus.switched_to == "original-target"


@pytest.mark.asyncio
async def test_open_magic_link_rejects_empty_link() -> None:
    result = await open_magic_link_in_new_tab(FakeBrowserSession(), "")

    assert result.status is MagicLinkOpenStatus.MISSING_LINK
    assert result.success is False
