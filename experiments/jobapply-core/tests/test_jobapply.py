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

import greenhouse_schema  # noqa: E402
import jobapply  # noqa: E402


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


if __name__ == "__main__":
    # Tiny runner so this works without pytest installed.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
