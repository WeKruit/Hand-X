"""Load ``.env`` before ``Settings()`` so Desktop-spawned binaries see ``GH_*`` vars.

The repo ``.env`` is not used when:

- PyInstaller sets ``sys.executable`` to the binary under
  ``~/Library/Application Support/gh-desktop-app/bin/`` (or equivalent), and
- the process cwd is not the Hand-X repo root.

We load, in order (``override=False`` — existing ``os.environ`` wins):

1. ``GH_ENV_FILE`` if set (absolute or ``~`` path).
2. ``.env`` next to the running executable (same directory as ``hand-x-darwin-arm64``, etc.).
3. ``<GH_DESKTOP_USER_DATA_PATH>/bin/.env`` or the default Desktop userData ``bin/.env``.
4. ``.env`` in the current working directory (terminal / IDE runs).
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _desktop_bin_dotenv_candidates() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("GH_DESKTOP_USER_DATA_PATH", "").strip()
    if override:
        paths.append(Path(override).expanduser() / "bin" / ".env")
        return paths
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        paths.append(home / "Library/Application Support/gh-desktop-app/bin/.env")
    elif system == "Linux":
        xdg = os.environ.get("XDG_DATA_HOME", str(home / ".local/share"))
        paths.append(Path(xdg) / "gh-desktop-app" / "bin" / ".env")
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            paths.append(Path(appdata) / "gh-desktop-app" / "bin" / ".env")
    return paths


def bootstrap_handx_dotenv() -> None:
    """Populate os.environ from dotenv files if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    candidates: list[Path] = []
    gh_env_file = os.environ.get("GH_ENV_FILE", "").strip()
    if gh_env_file:
        candidates.append(Path(gh_env_file).expanduser())

    exe = Path(getattr(sys, "executable", "") or "").resolve()
    if exe.name and exe.name not in {"python", "python3", "Python", "Python3"}:
        candidates.append(exe.parent / ".env")

    candidates.extend(_desktop_bin_dotenv_candidates())
    candidates.append(Path.cwd() / ".env")

    seen: set[Path] = set()
    for path in candidates:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        load_dotenv(path, override=False)
        logger.debug("handx.env_loaded", extra={"path": str(path)})


__all__ = ["bootstrap_handx_dotenv"]
