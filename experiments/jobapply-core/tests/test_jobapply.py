"""Offline unit tests — no browser, no network, no LLM. These run anywhere.

They cover the only logic this package actually owns: the reusable instruction
block and the profile->variables mapping used for cheap reruns. Everything else
is a thin call into browser-use and is exercised by the e2e flow (see README).
"""

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import greenhouse_schema  # noqa: E402
import jobapply  # noqa: E402
import vision_verify  # noqa: E402


class _DetectedVar:
    """Mimics browser_use DetectedVariable (only the fields the mapper reads)."""

    def __init__(self, fmt=None):
        self.format = fmt


def test_instructions_toggle_submit():
    fill_only = jobapply.build_instructions(submit=False)
    submitting = jobapply.build_instructions(submit=True)
    # fill-only must forbid submit absolutely and trigger done() at the Submit step
    assert "NEVER click Submit" in fill_only and "done(success=True)" in fill_only
    assert "click the final Submit/Apply button once to submit" in submitting
    # reCAPTCHA must route to HITL, not a loop
    assert "HITL" in fill_only and "reCAPTCHA" in fill_only
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


def test_greenhouse_parse_url():
    assert greenhouse_schema.parse_job_url(
        "https://job-boards.greenhouse.io/discord/jobs/8289766002"
    ) == ("discord", "8289766002")


def test_greenhouse_classify_offline():
    # the standard template + one custom dropdown + one open-ended textarea
    schema = {
        "questions": [
            {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
            {"label": "Resume", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
            {"label": "Why are you interested?", "required": True,
             "fields": [{"name": "question_1", "type": "textarea"}]},
            {"label": "Authorized to work?", "required": True, "fields": [
                {"name": "question_2", "type": "multi_value_single_select",
                 "values": [{"label": "Yes"}, {"label": "No"}]}]},
        ]
    }
    plan = greenhouse_schema.classify(schema, {"first_name": "Ruiyang"})
    by_name = {r["name"]: r for r in plan}
    assert by_name["first_name"]["source"] == "standard" and by_name["first_name"]["value"] == "Ruiyang"
    assert by_name["first_name"]["llm"] is False
    assert by_name["question_1"]["source"] == "open_ended" and by_name["question_1"]["llm"] is True
    assert by_name["question_2"]["source"] == "select" and by_name["question_2"]["options"] == ["Yes", "No"]
    s = greenhouse_schema.summarize(plan)
    assert s["llm_fields"] == 1 and s["total"] == 4  # only the open-ended question costs an LLM


# ---- vision_verify: visual-check cache + per-page VLM budget (no browser/VLM) -------


class _FakeSession:
    """Stands in for a browser-use BrowserSession: a fixed URL + a counting screenshot."""

    def __init__(self, url="https://job-boards.greenhouse.io/discord/jobs/1"):
        self._url = url
        self.shots = 0

    async def get_current_page_url(self):
        return self._url

    async def take_screenshot(self):
        self.shots += 1
        return b"\x89PNG-fake-bytes"


def _patch_vlm(verdict='{"filled": true, "value": "x"}'):
    """Replace the real ChatGoogle with a fake whose ainvoke increments a call counter."""
    box = {"calls": 0}

    class _Resp:
        completion = verdict

    class _FakeVLM:
        async def ainvoke(self, _msgs):
            box["calls"] += 1
            return _Resp()

    vision_verify._vlm = lambda: _FakeVLM()
    return box


def test_is_filled_parsing():
    f = vision_verify._is_filled
    assert f('{"filled": true, "value": "646-678-9391"}')
    assert f("{'filled':true}")           # single quotes / no spaces
    assert f('{"filled":   true }')       # extra whitespace
    assert not f('{"filled": false}')
    assert not f('{"filled": null, "capped": true}')
    assert not f('{"filled": null, "error": "screenshot"}')


def test_visual_check_caches_per_field_and_url():
    vision_verify.reset_visual_cache()
    box = _patch_vlm()
    session = _FakeSession()

    async def go():
        # same field (same key + url), two phrasings -> ONE VLM call, served from cache
        v1 = await vision_verify.visual_check(session, "Phone", key="phone")
        v2 = await vision_verify.visual_check(session, "the 'phone' field", key="phone")
        assert v1 == v2
        # a DIFFERENT page (url) is a distinct cache entry -> a second VLM call
        other = _FakeSession(url="https://job-boards.greenhouse.io/discord/jobs/2")
        await vision_verify.visual_check(other, "Phone", key="phone")
        return session.shots

    shots = asyncio.run(go())
    assert box["calls"] == 2 and shots == 1   # 1 screenshot on page 1 (2nd was cached), 1 on page 2


def test_visual_check_caps_vlm_calls_then_goes_silent():
    vision_verify.reset_visual_cache()
    box = _patch_vlm()
    old_cap = vision_verify.VLM_MAX_CALLS
    vision_verify.VLM_MAX_CALLS = 2
    try:
        session = _FakeSession()

        async def go():
            a = await vision_verify.visual_check(session, "f1", key="f1")
            b = await vision_verify.visual_check(session, "f2", key="f2")
            c = await vision_verify.visual_check(session, "f3", key="f3")  # over budget
            return a, b, c

        a, b, c = asyncio.run(go())
        assert vision_verify._is_filled(a) and vision_verify._is_filled(b)
        assert not vision_verify._is_filled(c) and '"capped": true' in c
        assert box["calls"] == 2  # the 3rd never hit the VLM
    finally:
        vision_verify.VLM_MAX_CALLS = old_cap


if __name__ == "__main__":
    # Tiny runner so this works without pytest installed.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
