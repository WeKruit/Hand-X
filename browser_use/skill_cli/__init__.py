"""Browser-use CLI package.

This package provides a fast command-line interface for browser automation.
The CLI uses a session server architecture for persistent browser sessions.

Usage:
    browser-use open https://example.com
    browser-use click 5
    browser-use type "Hello World"
    browser-use python "print(browser.url)"
    browser-use run "Fill the contact form"
    browser-use close
"""

from browser_use.skill_cli.main import main

__all__ = ['main']
