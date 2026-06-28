"""Greenhouse is a hard template — extract the form schema deterministically.

Every `job-boards.greenhouse.io/<org>/jobs/<id>` form is rendered from the same
template: the SAME 8 standard fields (first_name, last_name, email, phone, resume,
cover_letter) on every board, plus per-job custom `question_<id>` fields. The full
schema — field names, TYPES, and the allowed values for every dropdown — is public:

    GET https://boards-api.greenhouse.io/v1/boards/<org>/jobs/<id>?questions=true

So we never need an LLM (or even a browser) to DISCOVER the form. We pull the schema,
map the applicant profile onto it deterministically, and the ONLY fields that need a
model are genuinely open-ended free-text questions ("Why are you interested?"). That
is the cost win across ALL Greenhouse jobs from ONE adapter — no per-job recording,
no fragile replay.

This module owns the extraction + classification. It feeds jobapply: pass the plan
into the agent task so it fills a KNOWN map instead of exploring (fewer steps/$), or
drive a deterministic DOM fill for the standard fields and reserve the agent for the
open-ended ones.

    python greenhouse_schema.py https://job-boards.greenhouse.io/discord/jobs/8289766002 \
        [--profile fixtures/sample_profile.json]
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

API = "https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job_id}?questions=true"

# The standard template fields, keyed to profile keys. Same on every Greenhouse board.
STANDARD = {
    "first_name": "first_name",
    "last_name": "last_name",
    "preferred_name": "preferred_name",
    "email": "email",
    "phone": "phone",
    "resume": "__resume_upload__",  # file upload (PDF), not text
    "resume_text": None,  # skip — we upload the PDF
    "cover_letter": "__cover_letter_upload__",
    "cover_letter_text": "cover_letter",  # the textarea behind "Enter manually"
}

# Free-text question labels that are genuinely open-ended -> need an LLM answer.
_OPEN_ENDED = re.compile(r"why|describe|tell us|what (drew|motivat)|interest|cover|anything else", re.I)


def parse_job_url(url: str) -> tuple[str, str]:
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?token=|[^/]*/)?([\w-]+)/jobs/(\d+)", url)
    if not m:
        # job-boards.greenhouse.io/<org>/jobs/<id>
        m = re.search(r"/([\w-]+)/jobs/(\d+)", url)
    if not m:
        raise SystemExit(f"Could not parse org/job_id from {url!r}")
    return m.group(1), m.group(2)


def fetch_schema(org: str, job_id: str) -> dict[str, Any]:
    import ssl

    url = API.format(org=org, job_id=job_id)
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=15, context=ctx) as r:  # (trusted host)
        return json.loads(r.read().decode())


def _question_row(label: str, required: bool, f: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    name, ftype = f.get("name"), f.get("type")
    values = [v.get("label") for v in f.get("values", [])] or None
    # label -> option value id (needed to check the right checkbox when a select is
    # rendered as a checkbox list, e.g. Greenhouse multi_value_multi_select).
    value_map = {v.get("label"): str(v.get("value")) for v in f.get("values", []) if v.get("label")} or None
    row: dict[str, Any] = {
        "name": name,
        "type": ftype,
        "label": label,
        "required": required,
        "option_values": value_map,
    }
    if name in STANDARD:
        pk = STANDARD[name]
        row["source"] = "skip" if pk is None else "standard"
        row["value"] = None if pk is None or pk.startswith("__") else profile.get(pk)
        row["llm"] = False
    elif ftype == "input_hidden":
        row["source"] = "skip"  # longitude/latitude etc. — set by widgets, not us
        row["llm"] = False
    elif ftype in ("multi_value_single_select", "multi_value_multi_select") or values:
        row["source"] = "select"
        row["options"] = values
        row["llm"] = False
    elif ftype == "textarea" or _OPEN_ENDED.search(label):
        row["source"] = "open_ended"  # genuine free text -> needs the model
        row["llm"] = True
    else:
        row["source"] = "input_text"  # short custom question
        row["llm"] = not bool(profile)
    return row


def classify(schema: dict[str, Any], profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Turn the raw schema into a per-field plan: how each field gets answered and
    whether it costs an LLM call. Covers ALL of Greenhouse's field blocks — `questions`,
    `location_questions`, and `demographic_questions` (EEO) — not just `questions`."""
    profile = profile or {}
    plan: list[dict[str, Any]] = []

    # standard + custom questions, plus the separate Location block (same {label,fields} shape)
    for block in ("questions", "location_questions"):
        for q in schema.get(block) or []:
            label = (q.get("label") or "").strip()
            required = q.get("required", False)
            for f in q.get("fields", []):
                plan.append(_question_row(label, required, f, profile))

    # demographic / EEO block — different shape: each question has an id (== the DOM
    # react-select id) and answer_options (one flagged decline_to_answer = the privacy default).
    dq = schema.get("demographic_questions") or {}
    for q in dq.get("questions") or []:
        opts = q.get("answer_options") or []
        labels = [o.get("label") for o in opts if o.get("label")]
        plan.append(
            {
                "name": str(q.get("id")),
                "type": q.get("type", "multi_value_single_select"),
                "label": (q.get("label") or "").strip(),
                "required": q.get("required", False),
                "source": "select",
                "options": labels or None,
                "option_values": {o.get("label"): str(o.get("id")) for o in opts if o.get("label")} or None,
                "llm": False,
            }
        )
    return plan


def summarize(plan: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(plan),
        "standard_$0": sum(1 for r in plan if r["source"] == "standard"),
        "select_$0": sum(1 for r in plan if r["source"] == "select"),
        "llm_fields": sum(1 for r in plan if r.get("llm")),
        "skip": sum(1 for r in plan if r["source"] == "skip"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Extract a Greenhouse job's form schema + fill plan")
    p.add_argument("job_url")
    p.add_argument("--profile", default=None)
    args = p.parse_args()

    org, job_id = parse_job_url(args.job_url)
    schema = fetch_schema(org, job_id)
    profile = json.loads(Path(args.profile).read_text()) if args.profile else None
    plan = classify(schema, profile)

    print(f"# {org}/{job_id} — {schema.get('title', '')}")
    for r in plan:
        tag = "LLM" if r.get("llm") else "$0 "
        extra = f"  ({len(r['options'])} opts)" if r.get("options") else ""
        val = f"  -> {r['value']!r}" if r.get("value") is not None else ""
        print(f"  [{tag}] {r['source']:<10} {r['type']:<26} {r['name']:<22} {r['label'][:40]}{extra}{val}")
    s = summarize(plan)
    print(f"\nsummary: {s}")
    print(
        f"=> {s['llm_fields']} of {s['total']} fields need an LLM; the rest fill at $0 from the API schema + profile."
    )


if __name__ == "__main__":
    main()
