"""Local end-to-end harness for toy app email-code verification.

This proves the Phase 1 fake inbox and Phase 2 browser helpers work together
against a real browser page, without real Gmail, VALET, Desktop, or live ATS
targets.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from werkzeug import Response

pytest.importorskip("playwright.async_api")

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.email_verification import (
    EmailVerificationMode,
    EmailVerificationPageKind,
    EmailVerificationRecoveryConfig,
    EmailVerificationRecoveryStatus,
    FakeInboxClient,
    MailboxMessage,
    MailboxVerificationQuery,
    extract_email_verification_page_state,
    is_auto_resolvable_email_page,
    recover_email_verification_if_possible,
)

_TOY_APP = Path(__file__).resolve().parent.parent.parent / "examples" / "toy-job-app" / "index.html"
_APP_EMAIL = "candidate.qa@gmail.com"
_APP_PASSWORD = "correct-horse-battery-staple"
_VERIFICATION_CODE = "482913"


def _parse_json_body(request: Any) -> dict[str, Any]:
    raw = request.get_data(as_text=True) if hasattr(request, "get_data") else ""
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise AssertionError(f"Expected JSON object body, got {type(parsed).__name__}")
    return parsed


def _json_response(payload: dict[str, Any], *, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, content_type="application/json")


@asynccontextmanager
async def _managed_browser_session():
    session = BrowserSession(
        browser_profile=BrowserProfile(
            headless=True,
            user_data_dir=None,
            keep_alive=True,
            enable_default_extensions=True,
        )
    )
    await session.start()
    try:
        yield session
    finally:
        await session.kill()
        await session.event_bus.stop(clear=True, timeout=5)


async def _wait_for_js(page: Any, predicate_js: str, *, timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        raw = await page.evaluate(predicate_js)
        if str(raw).strip().lower() == "true":
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"Timed out waiting for browser predicate: {predicate_js}")


async def _read_text(page: Any, expression_js: str) -> str:
    return str(await page.evaluate(expression_js) or "")


def _install_toy_auth_routes(httpserver, *, requests_seen: list[tuple[str, dict[str, Any]]]) -> None:
    def start_handler(request):
        body = _parse_json_body(request)
        requests_seen.append(("start", body))
        if body.get("email") != _APP_EMAIL or not body.get("password"):
            return _json_response({"error": "invalid local test credentials"}, status=400)
        return _json_response({"ok": True})

    def verify_handler(request):
        body = _parse_json_body(request)
        requests_seen.append(("verify", body))
        if body.get("email") != _APP_EMAIL or body.get("code") != _VERIFICATION_CODE:
            return _json_response({"error": "invalid verification code"}, status=400)
        return _json_response({"ok": True})

    def resend_handler(request):
        body = _parse_json_body(request)
        requests_seen.append(("resend", body))
        return _json_response({"ok": True})

    httpserver.expect_request("/api/auth/google/start", method="POST").respond_with_handler(start_handler)
    httpserver.expect_request("/api/auth/google/verify", method="POST").respond_with_handler(verify_handler)
    httpserver.expect_request("/api/auth/google/resend", method="POST").respond_with_handler(resend_handler)
    httpserver.expect_request("/favicon.ico").respond_with_data("", status=204)


async def _submit_google_login(page: Any) -> None:
    await page.evaluate(
        """(payload) => {
        const setValue = (id, value) => {
          const el = document.getElementById(id);
          el.value = value;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        setValue('googleLoginEmail', payload.email);
        setValue('googleLoginPassword', payload.password);
        document.getElementById('googleLoginForm').dispatchEvent(
          new Event('submit', { bubbles: true, cancelable: true })
        );
      }""",
        {"email": _APP_EMAIL, "password": _APP_PASSWORD},
    )


def _fake_inbox_for_state(state_detected_at) -> FakeInboxClient:
    return FakeInboxClient(
        [
            MailboxMessage(
                message_id="toy-email-verification-code",
                received_at=state_detected_at + timedelta(seconds=1),
                sender="Acme Careers <no-reply@acme.example>",
                subject="Acme Careers verification code",
                recipients=(_APP_EMAIL,),
                body_text=f"Use verification code {_VERIFICATION_CODE} to continue your Acme Careers application.",
            )
        ]
    )


async def _select_best_fake_inbox_code(state) -> str:
    inbox = _fake_inbox_for_state(state.detected_at)
    query = MailboxVerificationQuery(
        application_email=state.application_email,
        connected_email=_APP_EMAIL,
        detected_at=state.detected_at,
        site_hostname=state.site_hostname,
        platform="toy-job-app",
        company_hint="Acme",
        expected_code_length=6,
    )
    candidates = await inbox.list_verification_candidates(query)
    assert candidates, "fake inbox should produce one verification candidate"
    return candidates[0].artifact_value


class _RuntimeFakeInboxClient:
    async def list_verification_candidates(self, query: MailboxVerificationQuery):
        inbox = FakeInboxClient(
            [
                MailboxMessage(
                    message_id="toy-runtime-email-verification-code",
                    received_at=query.detected_at + timedelta(seconds=1),
                    sender="Acme Careers <no-reply@acme.example>",
                    subject="Acme Careers verification code",
                    recipients=(_APP_EMAIL,),
                    body_text=f"Use verification code {_VERIFICATION_CODE} to continue your Acme Careers application.",
                )
            ]
        )
        return await inbox.list_verification_candidates(query)


@pytest.mark.asyncio
async def test_toy_app_email_code_verification_e2e(httpserver) -> None:
    """Toy app flow: login gate -> verification wall -> runtime fake inbox -> code fill -> app unlock."""

    assert _TOY_APP.is_file(), f"missing toy app fixture: {_TOY_APP}"
    requests_seen: list[tuple[str, dict[str, Any]]] = []
    httpserver.expect_request("/").respond_with_data(
        _TOY_APP.read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/index.html").respond_with_data(
        _TOY_APP.read_text(encoding="utf-8"),
        content_type="text/html; charset=utf-8",
    )
    _install_toy_auth_routes(httpserver, requests_seen=requests_seen)

    async with _managed_browser_session() as browser_session:
        await Tools().navigate(url=httpserver.url_for("/"), new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        await _submit_google_login(page)
        await _wait_for_js(page, "() => !document.getElementById('verificationGateCard').hidden")

        state = await extract_email_verification_page_state(page, platform="toy-job-app", company_hint="Acme")
        assert state.page_kind is EmailVerificationPageKind.EMAIL_CODE
        assert is_auto_resolvable_email_page(state)
        assert state.application_email == _APP_EMAIL
        assert state.code_input_selectors == ("#googleVerificationCode",)

        recovery_result = await recover_email_verification_if_possible(
            browser_session,
            blocker_text="blocker: email verification required -- user must verify email then retry",
            config=EmailVerificationRecoveryConfig(
                mode=EmailVerificationMode.FAKE_INBOX,
                application_email=_APP_EMAIL,
                connected_email=_APP_EMAIL,
                min_candidate_score=0.75,
                poll_attempts=1,
                poll_interval_seconds=0,
            ),
            inbox_client=_RuntimeFakeInboxClient(),
            platform="toy-job-app",
            company_hint="Acme",
        )
        assert recovery_result.status is EmailVerificationRecoveryStatus.RESOLVED_CODE
        assert recovery_result.resolved is True

        await _wait_for_js(page, "() => document.getElementById('applicationContainer').hidden === false")
        assert await _read_text(page, "() => document.getElementById('email').value") == _APP_EMAIL

    assert [name for name, _body in requests_seen] == ["start", "verify"]
    assert requests_seen[1][1]["code"] == _VERIFICATION_CODE
