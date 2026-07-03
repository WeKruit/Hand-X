"""wd_verify_email — gmail plus-alias inbox for Workday account verification codes.

Why: tenants blocklist mailinator-style throwaway domains (today's AUTH split: 4/8 creation
rejections) and some gate on an emailed code. ONE real gmail inbox + plus-aliases gives every
tenant a unique, real-domain address whose mail all lands in the same IMAP-readable inbox.

Config (env; absent = module disabled, callers fall back to the old path / HITL):
  GH_VERIFY_IMAP_USER  the real gmail address, e.g. wekruit.apply@gmail.com
  GH_VERIFY_IMAP_PASS  gmail APP password (never argv)
  GH_VERIFY_IMAP_HOST  default imap.gmail.com

stdlib only (imaplib + email). fetch_code is blocking — call via asyncio.to_thread.
"""

import email
import email.policy
import imaplib
import os
import re
import secrets
import time


def enabled() -> bool:
    return bool(os.environ.get("GH_VERIFY_IMAP_USER") and os.environ.get("GH_VERIFY_IMAP_PASS"))


def alias_email(tenant: str) -> str | None:
    """Unique real-domain address per tenant run: user+<tenant><rand>@gmail.com. None if disabled."""
    user = os.environ.get("GH_VERIFY_IMAP_USER", "")
    if not user or "@" not in user:
        return None
    local, dom = user.split("@", 1)
    tag = re.sub(r"[^a-z0-9]", "", tenant.lower())[:16]
    return f"{local}+{tag}{secrets.randbelow(9999):04d}@{dom}"


def _extract_code(subject: str, body: str) -> str | None:
    """The code is 4-8 digits near 'code'/'verif' wording; subject first (Workday puts it there),
    then body. A bare year ('2026') in unrelated text must NOT match — require the keyword within
    the same text blob."""
    for blob in (subject, body):
        if not blob or not re.search(r"verif|code|一次性|認証", blob, re.I):
            continue
        m = re.search(r"(?<!\d)(\d{4,8})(?!\d)", blob)
        if m:
            return m.group(1)
    return None


def fetch_code(to_addr: str, timeout_s: float = 150.0, poll_s: float = 6.0) -> str | None:
    """Poll the inbox for the newest message addressed to `to_addr` and return its code.
    BLOCKING (imaplib) — call via asyncio.to_thread. None on timeout/any error."""
    host = os.environ.get("GH_VERIFY_IMAP_HOST", "imap.gmail.com")
    user = os.environ.get("GH_VERIFY_IMAP_USER", "")
    pw = os.environ.get("GH_VERIFY_IMAP_PASS", "")
    if not (user and pw):
        return None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            box = imaplib.IMAP4_SSL(host)
            box.login(user, pw)
            box.select("INBOX")
            # gmail folds plus-aliases into one inbox; TO catches the alias on the envelope
            _, data = box.search(None, "TO", f'"{to_addr}"')
            ids = (data[0] or b"").split()
            for mid in reversed(ids[-5:]):  # newest few only
                _, msg_data = box.fetch(mid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
                body = ""
                part = msg.get_body(preferencelist=("plain", "html"))
                if part is not None:
                    body = str(part.get_content())[:4000]
                code = _extract_code(str(msg.get("Subject", "")), body)
                if code:
                    box.logout()
                    return code
            box.logout()
        except Exception as exc:
            print(f"   [verify-email] imap poll error: {exc}")
        time.sleep(poll_s)
    return None


if __name__ == "__main__":  # offline self-check
    assert _extract_code("Your Workday verification code is 483920", "") == "483920"
    assert _extract_code("", "Enter this code: 771204 to continue") == "771204"
    assert _extract_code("Job alert for 2026 graduates", "great roles this 2026 season") is None
    assert _extract_code("", "no digits here") is None
    os.environ["GH_VERIFY_IMAP_USER"] = "wekruit.apply@gmail.com"
    a = alias_email("Chewy")
    assert a and a.startswith("wekruit.apply+chewy") and a.endswith("@gmail.com")
    print("wd_verify_email self-check OK:", a)
