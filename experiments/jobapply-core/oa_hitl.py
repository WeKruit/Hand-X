"""oa_hitl — generic human-in-the-loop blocker gate (user goal: '遇到 blocker 如何 continue').

When the engine hits a wall it CANNOT clear itself — a CAPTCHA/slider, a login it lacks creds
for, an emailed verify code — the right move in production is not to fail: surface the blocker
to the human, let them clear it IN THE SAME BROWSER (the CDP-attached real Chrome the Desktop
app owns), then RESUME filling where we left off.

This is the resume half of the NEEDS_HUMAN story. failcap already captures + classifies the
blocker; code_source already relays a verify CODE. This adds the general case: pause until the
human ACTS in the browser and the blocker is gone, then continue.

Contract (one function): wait_for_unblock(session, page, *, kind, reason, still_blocked) -> bool
  - kind/reason: what the blocker is (from failcap triage) — shown to the human.
  - still_blocked(page) -> bool: a cheap re-check the caller supplies (e.g. 'is the CAPTCHA
    still on screen / is the form still absent'). We poll it; True when the human has cleared it.
  - Returns True if cleared within the window (caller RESUMES), False on timeout (caller halts
    NEEDS_HUMAN as before).

OPT-IN via GH_HITL=1 — an unattended sweep must never block for a human; it returns False
instantly so the run halts fast. Surfacing is file+console here (the reference a VALET/desktop
push implements later): writes runs/hitl/blocker.json and prints a banner; the human clears the
blocker in the browser, optionally touches runs/hitl/continue to force-resume.
"""

import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


def enabled() -> bool:
    return os.environ.get("GH_HITL") == "1"


async def wait_for_unblock(
    session: Any,
    page: Any,
    *,
    kind: str,
    reason: str,
    still_blocked: Callable[[Any], Awaitable[bool]],
    timeout_s: float | None = None,
) -> bool:
    """Pause until the human clears the blocker in the browser (or a force-continue signal), then
    return True so the caller resumes. False if disabled or the window elapses. NEVER raises."""
    if not enabled():
        return False
    timeout_s = timeout_s if timeout_s is not None else float(os.environ.get("GH_HITL_TIMEOUT_S", "600"))
    d = Path(os.environ.get("GH_HITL_DIR", "runs/hitl"))
    with contextlib.suppress(Exception):
        d.mkdir(parents=True, exist_ok=True)
    cont = d / "continue"
    with contextlib.suppress(Exception):
        cont.unlink()
    with contextlib.suppress(Exception):
        url = ""
        with contextlib.suppress(Exception):
            url = await page.get_url()
        (d / "blocker.json").write_text(
            json.dumps({"ts": time.strftime("%F %T"), "kind": kind, "reason": reason, "url": url}, indent=2)
        )
    print(
        f"   [hitl] BLOCKER ({kind}): {reason}\n"
        f"   [hitl] solve it in the browser, then it auto-resumes — or touch "
        f"{d / 'continue'} to force ({timeout_s:.0f}s)"
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cont.exists():  # human forced resume
            with contextlib.suppress(Exception):
                cont.unlink()
            print("   [hitl] human signalled continue")
            return True
        with contextlib.suppress(Exception):
            if not await still_blocked(page):  # human cleared it in the browser
                print("   [hitl] blocker cleared — resuming")
                return True
        with contextlib.suppress(Exception):
            import asyncio

            await asyncio.sleep(3)
    print("   [hitl] blocker not cleared within window — halting NEEDS_HUMAN")
    return False


if __name__ == "__main__":  # offline self-check
    import asyncio

    async def _main() -> None:
        os.environ.pop("GH_HITL", None)
        assert await wait_for_unblock(None, None, kind="CAPTCHA", reason="x", still_blocked=lambda p: _true()) is False

        os.environ.update(GH_HITL="1", GH_HITL_TIMEOUT_S="10", GH_HITL_DIR="runs/hitl_selftest2")

        calls = {"n": 0}

        async def blocked(_p: Any) -> bool:
            calls["n"] += 1
            return calls["n"] < 2  # "cleared" on the 2nd poll

        assert await wait_for_unblock(None, None, kind="CAPTCHA", reason="slider", still_blocked=blocked) is True
        print("oa_hitl self-check OK")

    async def _true() -> bool:
        return True

    asyncio.run(_main())
