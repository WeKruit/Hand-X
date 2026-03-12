# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Hand-X (GhostHands v2).

Packages the ghosthands + vendored browser_use into a single console binary
with the Playwright driver bundled. The Electron desktop app downloads and
spawns this binary -- no Python installation required on the user machine.

Targets: macOS ARM, macOS Intel, Windows x64, Linux x64.

Usage:
    pyinstaller build/hand-x.spec --distpath build/dist --workpath build/work --noconfirm
"""

import os
import platform
import sys
from pathlib import Path

from PyInstaller.building.api import EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_data_files

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(SPECPATH).parent  # project root (one level above build/)
GHOSTHANDS_PKG = ROOT / "ghosthands"
BROWSER_USE_PKG = ROOT / "browser_use"

# ---------------------------------------------------------------------------
# Data files -- non-Python assets that must be bundled
# ---------------------------------------------------------------------------

# browser_use system prompt templates (loaded via importlib.resources at runtime)
system_prompt_datas = [
    (str(BROWSER_USE_PKG / "agent" / "system_prompts" / "*.md"), os.path.join("browser_use", "agent", "system_prompts")),
]

# browser_use code_use system prompt
code_use_datas = [
    (str(BROWSER_USE_PKG / "code_use" / "system_prompt.md"), os.path.join("browser_use", "code_use")),
]

# browser_use MCP manifest
mcp_datas = [
    (str(BROWSER_USE_PKG / "mcp" / "manifest.json"), os.path.join("browser_use", "mcp")),
]

# Playwright driver (node binary + package/) -- collected by Playwright's own hook,
# but we declare it explicitly as a safety net.
playwright_datas = collect_data_files("playwright")

all_datas = system_prompt_datas + code_use_datas + mcp_datas + playwright_datas

# ---------------------------------------------------------------------------
# Hidden imports -- packages that PyInstaller can't detect statically
# ---------------------------------------------------------------------------

hidden_imports = [
    # ghosthands internals (dynamic imports in worker/agent)
    "ghosthands.config",
    "ghosthands.config.settings",
    "ghosthands.config.models",
    "ghosthands.agent",
    "ghosthands.actions",
    "ghosthands.dom",
    "ghosthands.platforms",
    "ghosthands.worker",
    "ghosthands.worker.poller",
    "ghosthands.integrations",
    "ghosthands.security",
    "ghosthands.llm",
    # browser_use internals
    "browser_use",
    "browser_use.agent",
    "browser_use.agent.service",
    "browser_use.agent.prompts",
    "browser_use.agent.views",
    "browser_use.agent.system_prompts",
    "browser_use.browser",
    "browser_use.browser.browser",
    "browser_use.browser.context",
    "browser_use.browser.profile",
    "browser_use.dom",
    "browser_use.dom.service",
    "browser_use.dom.views",
    "browser_use.code_use",
    "browser_use.code_use.service",
    "browser_use.filesystem",
    "browser_use.llm",
    "browser_use.llm.anthropic",
    "browser_use.llm.openai",
    "browser_use.llm.google",
    "browser_use.config",
    "browser_use.controller",
    "browser_use.sync",
    "browser_use.tokens",
    "browser_use.tools",
    "browser_use.actor",
    # Third-party runtime deps that use lazy/conditional imports
    "anthropic",
    "openai",
    "google.genai",
    "httpx",
    "httpx._transports",
    "httpx._transports.default",
    "aiohttp",
    "asyncpg",
    "asyncpg.protocol",
    "pydantic",
    "pydantic_settings",
    "structlog",
    "structlog.stdlib",
    "structlog.processors",
    "click",
    "rich",
    "rich.console",
    "rich.progress",
    "PIL",
    "PIL.Image",
    "cloudpickle",
    "markdownify",
    "posthog",
    "psutil",
    "pypdf",
    "docx",
    "dotenv",
    "pyotp",
    "portalocker",
    "uuid7",
    "anyio",
    "anyio._backends._asyncio",
    "authlib",
    "cdp_use",
    "bubus",
    "cryptography",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.ciphers.aead",
    # importlib.resources needs the package registered
    "importlib.resources",
    "importlib.metadata",
]

# ---------------------------------------------------------------------------
# Excludes -- packages we don't use; saves significant binary size
# ---------------------------------------------------------------------------

# LLM providers we never invoke (only anthropic + openai + google are needed)
excluded_modules = [
    "browser_use.llm.groq",
    "browser_use.llm.ollama",
    "browser_use.llm.cerebras",
    "browser_use.llm.deepseek",
    "browser_use.llm.mistral",
    "browser_use.llm.oci_raw",
    "browser_use.llm.aws",
    "browser_use.llm.azure",
    "browser_use.llm.openrouter",
    "browser_use.llm.vercel",
    "browser_use.llm.browser_use",
    "browser_use.llm.tests",
    # browser_use CLI (we have our own entry point)
    "browser_use.cli",
    "browser_use.init_cmd",
    "browser_use.skill_cli",
    # Their pip-installed counterparts (if present)
    "groq",
    "ollama",
    "cerebras",
    "deepseek",
    "mistralai",
    "oci",
    # Heavy stdlib/third-party modules we never use
    "tkinter",
    "unittest",
    "test",
    "email",
    "xmlrpc",
    "pydoc",
    "doctest",
    "lib2to3",
    "ensurepip",
    "idlelib",
    "turtledemo",
    "turtle",
    # Large optional dependencies
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "torch",
    "tensorflow",
    "IPython",
    "notebook",
    "jupyter",
    "setuptools",
    "pip",
    "wheel",
]

# ---------------------------------------------------------------------------
# Platform-specific settings
# ---------------------------------------------------------------------------

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# UPX causes issues on macOS and Linux -- only enable on Windows
use_upx = IS_WINDOWS
strip_binary = not IS_WINDOWS  # strip debug symbols on Unix

# macOS: set target arch from environment or detect
target_arch = None
if IS_MACOS:
    target_arch = os.environ.get("PYINSTALLER_TARGET_ARCH", platform.machine())

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    [str(ROOT / "ghosthands" / "cli.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=all_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=1,  # remove assert statements and __debug__ blocks
)

# ---------------------------------------------------------------------------
# PYZ archive (compiled Python bytecode)
# ---------------------------------------------------------------------------

pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# EXE -- single-file console executable
# ---------------------------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="hand-x",
    debug=False,
    bootloader_ignore_signals=False,
    strip=strip_binary,
    upx=use_upx,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # console mode -- Electron communicates via stdin/stdout
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # no icon for CLI binary
)
