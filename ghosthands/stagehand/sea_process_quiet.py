"""Suppress noisy JSON logs from the embedded Stagehand SEA Node server.

The stagehand-python package spawns a local binary (e.g. ``stagehand-darwin-arm64``)
with ``stdout=None`` / ``stderr=None``, so Fastify logs every HTTP request to the
parent terminal — unrelated to ``sessions.start(..., verbose=0)``.

We wrap ``subprocess.Popen`` only for that binary and send stdout/stderr to
``DEVNULL`` unless ``GH_STAGEHAND_SEA_LOGS`` is enabled.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

_installed = False


def _sea_logs_enabled() -> bool:
    v = (os.environ.get("GH_STAGEHAND_SEA_LOGS") or "").strip().lower()
    return v in ("1", "true", "yes", "verbose", "debug", "all")


def _cmd_is_stagehand_sea_binary(argv: Any) -> bool:
    if not argv:
        return False
    try:
        cmd0 = argv[0] if isinstance(argv, (list, tuple)) else argv
    except Exception:
        return False
    base = os.path.basename(str(cmd0)).lower()
    return base.startswith("stagehand-") or base == "stagehand.exe"


def install_sea_process_quiet() -> None:
    """Idempotent: redirect Stagehand SEA binary streams to DEVNULL when not debugging."""
    global _installed
    if _installed:
        return
    if _sea_logs_enabled():
        return

    _orig_popen = subprocess.Popen

    def _popen(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        if _cmd_is_stagehand_sea_binary(argv):
            kwargs = dict(kwargs)
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
            return _orig_popen(*args, **kwargs)
        return _orig_popen(*args, **kwargs)

    subprocess.Popen = _popen  # type: ignore[method-assign]
    _installed = True
