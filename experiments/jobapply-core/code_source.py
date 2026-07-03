"""code_source — pluggable verification-code sources (THE HITL interface).

The engine asks exactly one question: "a verification code was sent for <email> — what is
it?" WHO answers is a CodeSource behind one contract: an IMAP burner inbox (test lane), a
human relaying via VALET/desktop push, wekruit-pa as MCP reading the user's channels, an
SMS relay. Integration layers implement + register a source; the engine never changes.

Contract:
    class MySource:
        name = "valet"
        def enabled(self) -> bool: ...
        async def get_code(self, ctx: dict) -> str | None   # None = pass to next source
    code_source.register(MySource())

ctx keys (all optional strings): email, tenant, url.
Resolution order = GH_CODE_SOURCES env (comma list, default "imap,file"); first enabled
source returning a code wins. No enabled source -> None -> caller halts NEEDS_HUMAN as
before, so unattended sweeps stay fast.
"""

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol


class CodeSource(Protocol):
    name: str

    def enabled(self) -> bool: ...

    async def get_code(self, ctx: dict[str, Any]) -> str | None: ...


class ImapSource:
    """Test lane: burner-gmail plus-alias inbox (wd_verify_email). Enabled by GH_VERIFY_IMAP_*."""

    name = "imap"

    def enabled(self) -> bool:
        import wd_verify_email as wve

        return wve.enabled()

    async def get_code(self, ctx: dict[str, Any]) -> str | None:
        import wd_verify_email as wve

        email = ctx.get("email") or ""
        return await asyncio.to_thread(wve.fetch_code, email) if email else None


class FileSource:
    """Local HITL relay — the reference implementation of the SAME flow a VALET/desktop
    source performs later: surface a request (JSON file + console banner), poll for the
    human's answer (runs/hitl/code.txt; production polls the job's HITL API channel).
    OPT-IN via GH_HITL=1 — an unattended sweep must bail instantly, not wait 10 minutes."""

    name = "file"

    def enabled(self) -> bool:
        return os.environ.get("GH_HITL") == "1"

    async def get_code(self, ctx: dict[str, Any]) -> str | None:
        d = Path(os.environ.get("GH_HITL_DIR", "runs/hitl"))
        d.mkdir(parents=True, exist_ok=True)
        req, ans = d / "code_request.json", d / "code.txt"
        with contextlib.suppress(Exception):
            ans.unlink()
        req.write_text(json.dumps({"ts": time.strftime("%F %T"), **{k: str(v) for k, v in ctx.items()}}, indent=2))
        timeout = float(os.environ.get("GH_HITL_TIMEOUT_S", "600"))
        print(f"   [hitl] verification code needed for {ctx.get('email', '?')} — paste into {ans} ({timeout:.0f}s)")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ans.exists():
                code = "".join(ch for ch in ans.read_text() if ch.isdigit())
                with contextlib.suppress(Exception):
                    ans.unlink()
                if 4 <= len(code) <= 8:
                    return code
            await asyncio.sleep(2)
        return None


_REGISTRY: dict[str, CodeSource] = {"imap": ImapSource(), "file": FileSource()}


def register(source: CodeSource) -> None:
    """Integration layers (VALET channel, pa-MCP, SMS relay) plug in here."""
    _REGISTRY[source.name] = source


async def get_verification_code(ctx: dict[str, Any]) -> str | None:
    order = [s.strip() for s in os.environ.get("GH_CODE_SOURCES", "imap,file").split(",") if s.strip()]
    for name in order:
        src = _REGISTRY.get(name)
        if not src or not src.enabled():
            continue
        print(f"   [code-source] trying '{name}'")
        with contextlib.suppress(Exception):
            code = await src.get_code(ctx)
            if code:
                return code
    return None


if __name__ == "__main__":  # offline self-check

    async def _main() -> None:
        # 1. nothing enabled -> None fast (the unattended-sweep guarantee)
        os.environ.pop("GH_HITL", None)
        os.environ.pop("GH_VERIFY_IMAP_USER", None)
        assert await get_verification_code({"email": "x@y.z"}) is None

        # 2. custom source wins per GH_CODE_SOURCES order
        class Fake:
            name = "fake"

            def enabled(self) -> bool:
                return True

            async def get_code(self, ctx: dict[str, Any]) -> str | None:
                return "123456"

        register(Fake())
        os.environ["GH_CODE_SOURCES"] = "imap,fake,file"
        assert await get_verification_code({}) == "123456"

        # 3. FileSource round-trip: a concurrent writer answers the request
        os.environ.update(GH_HITL="1", GH_CODE_SOURCES="file", GH_HITL_TIMEOUT_S="15", GH_HITL_DIR="runs/hitl_selftest")

        async def human() -> None:
            await asyncio.sleep(0.5)
            Path("runs/hitl_selftest/code.txt").write_text("code: 9042 17\n")

        t = asyncio.create_task(human())
        assert await get_verification_code({"email": "a@b.c"}) == "904217"
        await t
        print("code_source self-check OK")

    asyncio.run(_main())
