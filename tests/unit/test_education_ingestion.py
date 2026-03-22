"""Focused regression tests for structured education ingestion."""

from __future__ import annotations

import json


def test_build_canonical_profile_joins_structured_education_text():
    from ghosthands.profile.canonical import build_canonical_profile

    profile = {
        "education": [
            {
                "school": "MIT",
                "degree": "Bachelor of Science",
                "degreeType": "Undergraduate",
                "major": ["Computer Science", "Mathematics"],
                "minors": ["Economics", "Statistics"],
                "honors": ["Phi Beta Kappa", "Summa Cum Laude"],
            }
        ]
    }

    canonical = build_canonical_profile(profile, {})

    assert canonical.get("degree_seeking") == "Bachelor of Science"
    assert canonical.get("degree_type") == "Undergraduate"
    assert canonical.get("field_of_study") == "Computer Science, Mathematics"
    assert canonical.get("minor") == "Economics, Statistics"
    assert canonical.get("honors") == "Phi Beta Kappa, Summa Cum Laude"


def test_load_resume_from_file_preserves_structured_education_fields(tmp_path):
    from ghosthands.integrations.resume_loader import load_resume_from_file

    payload = {
        "fullName": "Jane Major",
        "education": [
            {
                "school": "Berkeley",
                "degree": "Bachelor of Arts",
                "degreeType": "Undergraduate",
                "major": ["Computer Science", "Mathematics"],
                "minor": ["Statistics", "Philosophy"],
                "honors": ["Phi Beta Kappa", "Summa Cum Laude"],
                "fieldOfStudy": "Computer Science",
                "graduationDate": "2024-05",
            }
        ],
    }
    path = tmp_path / "resume.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    profile = load_resume_from_file(str(path))
    education = profile["education"][0]

    assert education["degree_type"] == "Undergraduate"
    assert education["field_of_study"] == "Computer Science"
    assert education["major"] == "Computer Science, Mathematics"
    assert education["minor"] == "Statistics, Philosophy"
    assert education["honors"] == "Phi Beta Kappa, Summa Cum Laude"


def test_load_resume_from_file_preserves_already_normalized_profile(tmp_path):
    from ghosthands.integrations.resume_loader import load_resume_from_file

    payload = {
        "first_name": "Ruiyang",
        "last_name": "Chen",
        "full_name": "Ruiyang Chen",
        "email": "rc5663@nyu.edu",
        "phone": "6466789391",
        "address": {
            "street": "",
            "city": "New York",
            "state": "NY",
            "zip": "10003",
            "country": "United States",
        },
        "education": [
            {
                "school": "New York University",
                "degree": "Bachelor of Science",
                "field_of_study": "Mathematics and Computer Science",
                "start_date": "2024-09",
                "end_date": "2027-05",
            }
        ],
    }
    path = tmp_path / "normalized-profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    profile = load_resume_from_file(str(path))

    assert profile["first_name"] == "Ruiyang"
    assert profile["last_name"] == "Chen"
    assert profile["full_name"] == "Ruiyang Chen"
    assert profile["address"]["city"] == "New York"
    assert profile["address"]["state"] == "NY"
    assert profile["address"]["zip"] == "10003"
