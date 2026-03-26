from __future__ import annotations

import asyncio


def test_load_runtime_profile_merges_resume_application_profile_and_answer_bank():
    from ghosthands.integrations.resume_loader import load_runtime_profile

    class StubDB:
        async def load_resume_profile_by_id(self, user_id: str, resume_id: str):
            assert user_id == "user-1"
            assert resume_id == "resume-1"
            return {
                "resume_id": "resume-1",
                "file_key": "resumes/user-1/resume-1.pdf",
                "parsing_confidence": 0.97,
                "raw_text": "Resume raw text",
                "parsed_data": {
                    "fullName": "Jane Doe",
                    "email": "jane@example.com",
                    "phone": "(646) 678-9391",
                    "location": "Austin, TX 78701",
                    "websites": [
                        "linkedin.com/in/jane-doe",
                        "janedoe.dev",
                    ],
                    "skills": ["Python", "SQL"],
                },
            }

        async def load_resume_profile(self, user_id: str):
            raise AssertionError("load_resume_profile() should not be used when resume_id is provided")

        async def load_user_profile(self, user_id: str):
            assert user_id == "user-1"
            return {
                "email": "fallback@example.com",
                "phone": "111-111-1111",
                "name": "Fallback User",
                "location": "Chicago, IL",
                "linkedin_url": "https://linkedin.com/in/fallback",
                "portfolio_url": "https://fallback.dev",
                "skills": ["Fallback Skill"],
            }

        async def load_user_application_profile(self, user_id: str):
            return {
                "work_authorization": "U.S. Citizen",
                "salary_expectation": "$120,000 base",
                "willing_to_relocate": "No",
            }

        async def load_resume_application_profile(self, user_id: str, resume_id: str):
            assert resume_id == "resume-1"
            return {
                "address": "123 Main St",
                "city": "Austin",
                "state": "TX",
                "zip_code": "78759",
                "county": "Travis County",
                "willing_to_relocate": "Yes",
                "availability_window": "Within 2 weeks",
            }

        async def load_answer_bank(self, user_id: str):
            return [
                {
                    "question": "Gender",
                    "answer": "Prefer not to say",
                    "canonical_question": "gender",
                    "intent_tag": "identity",
                    "usage_mode": "always_use",
                    "source": "user_input",
                    "confidence": "exact",
                    "synonyms": ["Sex"],
                }
            ]

    profile = asyncio.run(load_runtime_profile(StubDB(), "user-1", "resume-1"))

    assert profile["first_name"] == "Jane"
    assert profile["last_name"] == "Doe"
    assert profile["email"] == "jane@example.com"
    assert profile["linkedin_url"] == "https://linkedin.com/in/jane-doe"
    assert profile["website_url"] == "https://janedoe.dev"
    assert profile["work_authorization"] == "U.S. Citizen"
    assert profile["salary_expectation"] == "$120,000 base"
    assert profile["willing_to_relocate"] == "Yes"
    assert profile["availability_window"] == "Within 2 weeks"
    assert profile["city"] == "Austin"
    assert profile["state"] == "TX"
    assert profile["zip"] == "78759"
    assert profile["postal_code"] == "78759"
    assert profile["county"] == "Travis County"
    assert isinstance(profile["address"], dict)
    assert profile["address"]["street"] == "123 Main St"
    assert profile["answerBank"][0]["question"] == "Gender"
    assert profile["answer_bank"][0]["answer"] == "Prefer not to say"
    assert profile["_resume_id"] == "resume-1"


def test_load_runtime_profile_uses_default_resume_and_preserves_global_defaults_when_resume_override_is_blank():
    from ghosthands.integrations.resume_loader import load_runtime_profile

    class StubDB:
        async def load_resume_profile_by_id(self, user_id: str, resume_id: str):
            raise AssertionError("load_resume_profile_by_id() should not be used when resume_id is omitted")

        async def load_resume_profile(self, user_id: str):
            assert user_id == "user-2"
            return {
                "resume_id": "resume-default",
                "file_key": "resumes/user-2/default.pdf",
                "parsing_confidence": 0.91,
                "raw_text": "Default resume raw text",
                "parsed_data": {
                    "fullName": "John Example",
                    "email": "john@example.com",
                    "phone": "(212) 555-0100",
                    "location": "Seattle, WA 98101",
                },
            }

        async def load_user_profile(self, user_id: str):
            return {
                "email": "fallback@example.com",
                "name": "John Example",
                "location": "Seattle, WA",
            }

        async def load_user_application_profile(self, user_id: str):
            return {
                "work_authorization": "Authorized to work in the U.S.",
                "salary_expectation": "$150,000 base",
                "willing_to_relocate": "No",
                "availability_window": "Immediately",
            }

        async def load_resume_application_profile(self, user_id: str, resume_id: str):
            assert user_id == "user-2"
            assert resume_id == "resume-default"
            return {
                "willing_to_relocate": "",
                "availability_window": None,
                "county": "King County",
            }

        async def load_answer_bank(self, user_id: str):
            return []

    profile = asyncio.run(load_runtime_profile(StubDB(), "user-2"))

    assert profile["_resume_id"] == "resume-default"
    assert profile["work_authorization"] == "Authorized to work in the U.S."
    assert profile["salary_expectation"] == "$150,000 base"
    assert profile["willing_to_relocate"] == "No"
    assert profile["availability_window"] == "Immediately"
    assert profile["county"] == "King County"


def test_load_runtime_profile_accepts_global_languages_as_json_string():
    from ghosthands.integrations.resume_loader import load_runtime_profile

    class StubDB:
        async def load_resume_profile_by_id(self, user_id: str, resume_id: str):
            raise AssertionError("load_resume_profile_by_id() should not be used when resume_id is omitted")

        async def load_resume_profile(self, user_id: str):
            return {
                "resume_id": "resume-default",
                "file_key": "resumes/user-3/default.pdf",
                "parsing_confidence": 0.91,
                "raw_text": "Default resume raw text",
                "parsed_data": {
                    "fullName": "John Example",
                    "email": "john@example.com",
                },
            }

        async def load_user_profile(self, user_id: str):
            return {
                "email": "fallback@example.com",
                "name": "John Example",
            }

        async def load_user_application_profile(self, user_id: str):
            return {
                "spoken_languages": "English (Native / bilingual)",
                "languages": (
                    '[{"language":"English","overall_proficiency":"Native / bilingual",'
                    '"reading":"Native / bilingual","writing":"Native / bilingual","speaking":"Native / bilingual"},'
                    '{"language":"Mandarin","overall_proficiency":"Conversational",'
                    '"reading":"Conversational","writing":"Conversational","speaking":"Conversational"}]'
                ),
            }

        async def load_resume_application_profile(self, user_id: str, resume_id: str):
            return None

        async def load_answer_bank(self, user_id: str):
            return []

    profile = asyncio.run(load_runtime_profile(StubDB(), "user-3"))

    assert isinstance(profile["languages"], list)
    assert profile["languages"][0]["language"] == "English"
    assert profile["languages"][1]["language"] == "Mandarin"
    assert profile["spoken_languages"] == "English (Native / bilingual)"
