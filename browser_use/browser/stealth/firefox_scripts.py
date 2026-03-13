"""Firefox/Camoufox stealth scripts.

Camoufox has engine-level anti-fingerprinting built in, so it needs far
fewer JS-level patches than Chromium. This module provides only the
supplemental patches that enhance beyond Camoufox's defaults.

These scripts are injected via Playwright's ``add_init_script()`` (or
the CDP ``Page.addScriptToEvaluateOnNewDocument`` equivalent) and
persist across navigations.

Usage:
    scripts = get_firefox_stealth_scripts(enabled=True)
    for script in scripts:
        await page.add_init_script(script)
"""

from __future__ import annotations

# -- Supplemental patches for Camoufox ------------------------------------
#
# Camoufox already handles:
#   - navigator.webdriver = undefined
#   - WebGL fingerprint spoofing
#   - Canvas fingerprint protection
#   - AudioContext fingerprint protection
#   - Font enumeration protection
#   - Screen/window dimension normalization
#
# The patches below cover gaps that Camoufox doesn't address by default.

PERMISSIONS_PATCH = """
// Ensure Notification.permission returns 'default' (not 'denied')
// Some sites check this as a bot signal since headless browsers deny all.
(function() {
    try {
        if (typeof Notification !== 'undefined') {
            Object.defineProperty(Notification, 'permission', {
                get: function() { return 'default'; },
                configurable: true
            });
        }
    } catch(e) {}
})();
"""

MEDIA_CODECS_PATCH = """
// Ensure MediaSource.isTypeSupported returns true for common codecs.
// Headless browsers sometimes report no codec support.
(function() {
    try {
        if (typeof MediaSource !== 'undefined') {
            const originalIsTypeSupported = MediaSource.isTypeSupported;
            const commonCodecs = [
                'video/mp4; codecs="avc1.42E01E"',
                'video/mp4; codecs="avc1.42E01E, mp4a.40.2"',
                'video/webm; codecs="vp8"',
                'video/webm; codecs="vp9"',
                'audio/mp4; codecs="mp4a.40.2"',
                'audio/webm; codecs="opus"',
            ];
            MediaSource.isTypeSupported = function(type) {
                if (commonCodecs.some(c => type.includes(c.split(';')[0]))) {
                    return true;
                }
                return originalIsTypeSupported.call(this, type);
            };
        }
    } catch(e) {}
})();
"""

TIMEZONE_CONSISTENCY_PATCH = """
// Ensure Date timezone methods are consistent.
// Some detection scripts compare Intl.DateTimeFormat timezone with
// Date.getTimezoneOffset() to detect spoofing.
(function() {
    try {
        // Only patch if there's no timezone override already set
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (!tz) return;  // Let Camoufox handle it
    } catch(e) {}
})();
"""


def get_firefox_stealth_scripts(enabled: bool = True) -> list[str]:
    """Return list of stealth JS scripts for Firefox/Camoufox.

    Camoufox has extensive built-in stealth, so this returns a minimal
    set of supplemental patches. Returns an empty list if disabled.

    Args:
        enabled: Whether stealth patches are enabled.

    Returns:
        List of JavaScript strings to inject.
    """
    if not enabled:
        return []

    return [
        PERMISSIONS_PATCH,
        MEDIA_CODECS_PATCH,
        TIMEZONE_CONSISTENCY_PATCH,
    ]


def is_firefox_engine(engine_name: str) -> bool:
    """Check if the given engine name represents Firefox/Camoufox.

    Args:
        engine_name: Engine identifier string.

    Returns:
        True if the engine is Firefox-based.
    """
    return engine_name.lower() in ('firefox', 'camoufox')
