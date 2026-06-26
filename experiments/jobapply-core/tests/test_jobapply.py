"""Offline unit tests — no browser, no network, no LLM. These run anywhere.

They cover the only logic this package actually owns: the reusable instruction
block and the profile->variables mapping used for cheap reruns. Everything else
is a thin call into browser-use and is exercised by the e2e flow (see README).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import jobapply  # noqa: E402


class _DetectedVar:
    """Mimics browser_use DetectedVariable (only the fields the mapper reads)."""

    def __init__(self, fmt=None):
        self.format = fmt


def test_instructions_toggle_submit():
    fill_only = jobapply.build_instructions(submit=False)
    submitting = jobapply.build_instructions(submit=True)
    assert "Do NOT click the final Submit" in fill_only
    assert "click the final Submit/Apply button to submit" in submitting
    # conventions that must always be present (align with browser-use's manual)
    for must in ("get_recent_emails", "Next / Continue", "Never fabricate", "react-select"):
        assert must in fill_only


def test_map_profile_by_variable_name():
    detected = {"email": _DetectedVar(), "phone": _DetectedVar(), "first_name": _DetectedVar()}
    profile = {"email": "a@b.com", "phone": "+1 555", "first_name": "Jordan", "last_name": "Avery"}
    out = jobapply.map_profile_to_variables(detected, profile)
    assert out == {"email": "a@b.com", "phone": "+1 555", "first_name": "Jordan"}


def test_map_profile_by_format_hint():
    # variable name is opaque, but the detected format hint drives the match
    detected = {"field_7": _DetectedVar(fmt="email")}
    out = jobapply.map_profile_to_variables(detected, {"email": "x@y.com"})
    assert out == {"field_7": "x@y.com"}


def test_map_skips_unknown_and_missing():
    detected = {"captcha_token": _DetectedVar(), "email": _DetectedVar()}
    out = jobapply.map_profile_to_variables(detected, {"email": "a@b.com"})  # no captcha in profile
    assert out == {"email": "a@b.com"}


def test_sample_profile_is_valid_json():
    data = json.loads((HERE.parent / "fixtures" / "sample_profile.json").read_text())
    assert data["email"] and data["first_name"]


if __name__ == "__main__":
    # Tiny runner so this works without pytest installed.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
