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
