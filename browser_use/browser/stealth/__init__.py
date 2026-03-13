"""Stealth scripts for browser anti-detection.

Engine-specific stealth patches are in separate modules:
- scripts.py: Chromium-focused stealth JS injection patches
- firefox_scripts.py: Minimal patches for Camoufox (most stealth is engine-level)
"""

from browser_use.browser.stealth.config import StealthConfig
from browser_use.browser.stealth.scripts import get_stealth_scripts

__all__ = ['StealthConfig', 'get_stealth_scripts']
