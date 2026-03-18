"""Integration tests for the bridge profile adapter — real data, no mocks.

Tests the full camelCase-to-snake_case conversion pipeline that transforms
Desktop app ``UserProfile`` payloads into the format DomHand expects.

Every test uses realistic Desktop app payloads and calls the real
``camel_to_snake_profile`` and ``normalize_profile_defaults`` functions
directly.  Nothing is patched or mocked.
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from ghosthands.bridge.profile_adapter import (
    DOMHAND_PROFILE_DEFAULTS,
    camel_to_snake_profile,
    normalize_profile_defaults,
)

# ---------------------------------------------------------------------------
# Realistic Desktop app payload — matches the TypeScript UserProfile type
# ---------------------------------------------------------------------------

REAL_DESKTOP_PROFILE: dict[str, Any] = {
    "firstName": "John",
    "lastName": "Doe",
    "email": "john@example.com",
    "phone": "+14155551234",
    "phoneDeviceType": "Mobile",
    "phoneCountryCode": "+1",
    "linkedIn": "https://linkedin.com/in/johndoe",
    "zipCode": "94107",
    "workAuthorization": "Yes",
    "visaSponsorship": "No",
    "veteranStatus": "I am not a protected veteran",
    "disabilityStatus": "No, I Don't Have A Disability",
    "gender": "Male",
    "raceEthnicity": "Asian (Not Hispanic or Latino)",
    "education": [
        {
            "school": "MIT",
            "degree": "B.S.",
            "fieldOfStudy": "Computer Science",
            "graduationDate": "2020-05",
        },
    ],
    "experience": [
        {
            "company": "Google",
            "title": "Software Engineer",
            "startDate": "2020-06",
            "endDate": "2023-12",
        },
    ],
}


def _full_pipeline(profile: dict[str, Any]) -> dict[str, Any]:
    """Run both pipeline stages: camel-to-snake then normalize defaults."""
    return normalize_profile_defaults(camel_to_snake_profile(profile))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullDesktopProfileRoundTrip:
    """Take a realistic Desktop UserProfile, run the full pipeline, verify
    ALL fields are present and correct for DomHand consumption."""

    def test_scalar_fields_converted(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"
        assert result["email"] == "john@example.com"
        assert result["phone"] == "+14155551234"
        assert result["phone_device_type"] == "Mobile"
        assert result["phone_country_code"] == "+1"
        assert result["linkedin"] == "https://linkedin.com/in/johndoe"
        assert result["zip"] == "94107"
        assert result["postal_code"] == "94107"
        assert result["work_authorization"] == "Yes"
        assert result["visa_sponsorship"] == "No"
        assert result["veteran_status"] == "I am not a protected veteran"
        assert result["disability_status"] == "No, I Don't Have A Disability"
        assert result["gender"] == "Male"
        assert result["race_ethnicity"] == "Asian (Not Hispanic or Latino)"

    def test_nested_education_converted(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        edu = result["education"][0]
        assert edu["school"] == "MIT"
        assert edu["degree"] == "B.S."
        assert edu["field_of_study"] == "Computer Science"
        assert edu["graduation_date"] == "2020-05"
        # Original camelCase keys are preserved
        assert edu["fieldOfStudy"] == "Computer Science"
        assert edu["graduationDate"] == "2020-05"

    def test_nested_experience_converted(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        exp = result["experience"][0]
        assert exp["company"] == "Google"
        assert exp["title"] == "Software Engineer"
        assert exp["start_date"] == "2020-06"
        assert exp["end_date"] == "2023-12"
        # Originals preserved
        assert exp["startDate"] == "2020-06"
        assert exp["endDate"] == "2023-12"

    def test_original_camel_keys_preserved(self):
        """camel_to_snake_profile adds snake_case without removing originals."""
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        assert result["firstName"] == "John"
        assert result["lastName"] == "Doe"
        assert result["phoneDeviceType"] == "Mobile"
        assert result["zipCode"] == "94107"

    def test_address_defaults_filled(self):
        """Desktop profile has no explicit address dict — defaults should be applied."""
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        assert isinstance(result["address"], dict)
        assert result["address"]["country"] == "United States of America"


class TestProfileWithAllFieldsPopulated:
    """A complete profile where every field is set. Verify no defaults
    override the user's actual values."""

    def test_no_defaults_override_user_values(self):
        profile: dict[str, Any] = {
            "firstName": "Jane",
            "lastName": "Smith",
            "email": "jane@smith.com",
            "phone": "+442071234567",
            "phoneDeviceType": "Landline",
            "phoneCountryCode": "+44",
            "linkedIn": "https://linkedin.com/in/janesmith",
            "zipCode": "SW1A 1AA",
            "workAuthorization": "No",
            "visaSponsorship": "Yes",
            "veteranStatus": "I am a protected veteran",
            "disabilityStatus": "Yes, I Have A Disability",
            "gender": "Female",
            "raceEthnicity": "White (Not Hispanic or Latino)",
            "address": {
                "street": "10 Downing St",
                "city": "London",
                "state": "England",
                "zip": "SW1A 1AA",
                "country": "United Kingdom",
            },
            "education": [
                {
                    "school": "Oxford",
                    "degree": "M.A.",
                    "fieldOfStudy": "Philosophy",
                    "graduationDate": "2018-06",
                },
            ],
            "experience": [
                {
                    "company": "BBC",
                    "title": "Producer",
                    "startDate": "2018-09",
                    "endDate": "2024-01",
                },
            ],
        }
        result = _full_pipeline(profile)

        # Every field should keep the user's value, not a default
        assert result["phone_device_type"] == "Landline"
        assert result["phone_country_code"] == "+44"
        assert result["work_authorization"] == "No"
        assert result["visa_sponsorship"] == "Yes"
        assert result["veteran_status"] == "I am a protected veteran"
        assert result["disability_status"] == "Yes, I Have A Disability"
        assert result["gender"] == "Female"
        assert result["race_ethnicity"] == "White (Not Hispanic or Latino)"

        # Address should be the user's, not defaults
        assert result["address"]["street"] == "10 Downing St"
        assert result["address"]["city"] == "London"
        assert result["address"]["country"] == "United Kingdom"


class TestProfileWithMinimalFields:
    """Only firstName, lastName, email. Verify only structural defaults are filled."""

    def test_defaults_applied(self):
        profile: dict[str, Any] = {
            "firstName": "Min",
            "lastName": "Imal",
            "email": "min@imal.com",
        }
        result = _full_pipeline(profile)

        assert result["first_name"] == "Min"
        assert result["last_name"] == "Imal"
        assert result["email"] == "min@imal.com"

        # Structural defaults must be present
        assert result["phone_device_type"] == DOMHAND_PROFILE_DEFAULTS["phone_device_type"]
        assert result["phone_country_code"] == DOMHAND_PROFILE_DEFAULTS["phone_country_code"]
        assert "work_authorization" not in result
        assert "visa_sponsorship" not in result
        assert "veteran_status" not in result
        assert "disability_status" not in result
        assert "gender" not in result
        assert "race_ethnicity" not in result

        # Address should be the full default dict
        assert isinstance(result["address"], dict)
        assert result["address"]["country"] == "United States of America"


class TestProfileWithEducationAndExperience:
    """Nested arrays with camelCase dates. Verify snake_case conversion."""

    def test_multiple_education_entries(self):
        profile: dict[str, Any] = {
            "firstName": "Alice",
            "lastName": "Edu",
            "email": "alice@edu.com",
            "education": [
                {
                    "school": "Stanford",
                    "degree": "B.A.",
                    "fieldOfStudy": "Economics",
                    "graduationDate": "2015-06",
                },
                {
                    "school": "Harvard",
                    "degree": "MBA",
                    "fieldOfStudy": "Business Administration",
                    "graduationDate": "2018-05",
                },
            ],
        }
        result = _full_pipeline(profile)

        assert len(result["education"]) == 2
        for edu in result["education"]:
            assert "field_of_study" in edu
            assert "graduation_date" in edu
            # Originals preserved
            assert "fieldOfStudy" in edu
            assert "graduationDate" in edu

        assert result["education"][0]["field_of_study"] == "Economics"
        assert result["education"][1]["field_of_study"] == "Business Administration"

    def test_multiple_experience_entries(self):
        profile: dict[str, Any] = {
            "firstName": "Bob",
            "lastName": "Exp",
            "email": "bob@exp.com",
            "experience": [
                {
                    "company": "Meta",
                    "title": "SWE",
                    "startDate": "2019-01",
                    "endDate": "2021-06",
                },
                {
                    "company": "Apple",
                    "title": "Senior SWE",
                    "startDate": "2021-07",
                    "endDate": "2024-03",
                },
            ],
        }
        result = _full_pipeline(profile)

        assert len(result["experience"]) == 2
        for exp in result["experience"]:
            assert "start_date" in exp
            assert "end_date" in exp

        assert result["experience"][0]["start_date"] == "2019-01"
        assert result["experience"][1]["end_date"] == "2024-03"


class TestProfileAddressMerge:
    """Desktop sends partial address (just city and state). Verify merge
    with defaults."""

    def test_partial_address_fills_missing_fields(self):
        profile: dict[str, Any] = {
            "firstName": "City",
            "lastName": "State",
            "email": "cs@test.com",
            "address": {
                "city": "San Francisco",
                "state": "CA",
            },
        }
        result = _full_pipeline(profile)

        addr = result["address"]
        assert addr["city"] == "San Francisco"
        assert addr["state"] == "CA"
        # Defaults fill in the rest
        assert addr["country"] == "United States of America"
        assert addr["street"] == ""
        assert addr["zip"] == ""

    def test_partial_address_does_not_overwrite_provided(self):
        profile: dict[str, Any] = {
            "firstName": "Addr",
            "lastName": "Test",
            "email": "a@t.com",
            "address": {
                "city": "Boston",
                "state": "MA",
                "country": "USA",
            },
        }
        result = _full_pipeline(profile)

        # User's explicit country is kept, not overwritten by default
        assert result["address"]["country"] == "USA"
        assert result["address"]["city"] == "Boston"


class TestProfileStringAddressPreserved:
    """Desktop sends address as a string. Verify it's preserved as-is."""

    def test_string_address_not_overwritten(self):
        profile: dict[str, Any] = {
            "firstName": "Str",
            "lastName": "Addr",
            "email": "str@addr.com",
            "address": "San Francisco, CA",
        }
        result = _full_pipeline(profile)

        assert result["address"] == "San Francisco, CA"

    def test_string_address_still_gets_scalar_defaults(self):
        """Other defaults should still be applied even with a string address."""
        profile: dict[str, Any] = {
            "firstName": "Str",
            "lastName": "Addr",
            "email": "str@addr.com",
            "address": "NYC, NY 10001",
        }
        result = _full_pipeline(profile)

        assert result["address"] == "NYC, NY 10001"
        # Structural defaults still applied
        assert result["phone_device_type"] == "Mobile"
        assert "work_authorization" not in result


class TestProfilePreservesUnknownFields:
    """Desktop sends extra fields not in the mapping. Verify passthrough."""

    def test_custom_fields_pass_through(self):
        profile: dict[str, Any] = {
            "firstName": "Extra",
            "lastName": "Fields",
            "email": "extra@fields.com",
            "customField1": "custom_value_1",
            "preferredName": "Ex",
            "githubUrl": "https://github.com/extra",
        }
        result = _full_pipeline(profile)

        assert result["customField1"] == "custom_value_1"
        assert result["preferredName"] == "Ex"
        assert result["githubUrl"] == "https://github.com/extra"


class TestIdempotentDoubleConversion:
    """Run the full pipeline twice. Verify the output is identical both times."""

    def test_double_conversion_is_stable(self):
        first_pass = _full_pipeline(copy.deepcopy(REAL_DESKTOP_PROFILE))
        second_pass = _full_pipeline(copy.deepcopy(first_pass))

        assert first_pass == second_pass

    def test_double_conversion_minimal_profile(self):
        minimal: dict[str, Any] = {
            "firstName": "Idem",
            "lastName": "Potent",
            "email": "idem@potent.com",
        }
        first_pass = _full_pipeline(copy.deepcopy(minimal))
        second_pass = _full_pipeline(copy.deepcopy(first_pass))

        assert first_pass == second_pass


class TestZipCodeMapsToBothKeys:
    """Verify zipCode creates both ``zip`` and ``postal_code`` keys."""

    def test_zip_and_postal_code_both_set(self):
        profile: dict[str, Any] = {
            "firstName": "Zip",
            "lastName": "Code",
            "email": "zip@code.com",
            "zipCode": "90210",
        }
        result = _full_pipeline(profile)

        assert result["zip"] == "90210"
        assert result["postal_code"] == "90210"

    def test_existing_postal_code_not_overwritten(self):
        profile: dict[str, Any] = {
            "firstName": "Zip",
            "lastName": "Code",
            "email": "zip@code.com",
            "zipCode": "90210",
            "postal_code": "custom-postal",
        }
        result = _full_pipeline(profile)

        # zip should still come from zipCode
        assert result["zip"] == "90210"
        # existing postal_code should not be overwritten
        assert result["postal_code"] == "custom-postal"


class TestEmptyStringFieldsGetDefaults:
    """Profile has fields set to empty string. Verify only structural defaults are filled."""

    def test_empty_strings_replaced(self):
        profile: dict[str, Any] = {
            "firstName": "Empty",
            "lastName": "Strings",
            "email": "empty@strings.com",
            "phone_device_type": "",
            "phone_country_code": "",
            "work_authorization": "",
            "visa_sponsorship": "",
            "veteran_status": "",
            "disability_status": "",
            "gender": "",
            "race_ethnicity": "",
        }
        result = _full_pipeline(profile)

        assert result["phone_device_type"] == DOMHAND_PROFILE_DEFAULTS["phone_device_type"]
        assert result["phone_country_code"] == DOMHAND_PROFILE_DEFAULTS["phone_country_code"]
        assert result["work_authorization"] == ""
        assert result["visa_sponsorship"] == ""
        assert result["veteran_status"] == ""
        assert result["disability_status"] == ""
        assert result["gender"] == ""
        assert result["race_ethnicity"] == ""

    def test_empty_string_address_gets_defaults(self):
        profile: dict[str, Any] = {
            "firstName": "Empty",
            "lastName": "Addr",
            "email": "empty@addr.com",
            "address": "",
        }
        result = _full_pipeline(profile)

        assert isinstance(result["address"], dict)
        assert result["address"]["country"] == "United States of America"


class TestNoneFieldsGetDefaults:
    """Profile has fields set to None. Verify only structural defaults are filled."""

    def test_none_values_replaced(self):
        profile: dict[str, Any] = {
            "firstName": "None",
            "lastName": "Values",
            "email": "none@values.com",
            "phone_device_type": None,
            "phone_country_code": None,
            "work_authorization": None,
            "visa_sponsorship": None,
            "veteran_status": None,
            "disability_status": None,
            "gender": None,
            "race_ethnicity": None,
        }
        result = _full_pipeline(profile)

        assert result["phone_device_type"] == DOMHAND_PROFILE_DEFAULTS["phone_device_type"]
        assert result["phone_country_code"] == DOMHAND_PROFILE_DEFAULTS["phone_country_code"]
        assert result["work_authorization"] is None
        assert result["visa_sponsorship"] is None
        assert result["veteran_status"] is None
        assert result["disability_status"] is None
        assert result["gender"] is None
        assert result["race_ethnicity"] is None

    def test_none_address_gets_defaults(self):
        profile: dict[str, Any] = {
            "firstName": "None",
            "lastName": "Addr",
            "email": "none@addr.com",
            "address": None,
        }
        result = _full_pipeline(profile)

        assert isinstance(result["address"], dict)
        assert result["address"]["country"] == "United States of America"


class TestProfilePipelineMatchesDesktopContract:
    """The most important test: verify the full pipeline output has every
    field that DomHand's ``_parse_profile_evidence`` and
    ``_known_profile_value`` expect."""

    # Fields the pipeline guarantees via camelCase mapping + structural defaults.
    # Note: "linkedin" and "zip" only appear when the Desktop app sends
    # "linkedIn" / "zipCode" — they are NOT defaults.
    DOMHAND_GUARANTEED_FIELDS: ClassVar[set[str]] = {
        "first_name",
        "last_name",
        "email",
        "phone",
        "phone_device_type",
        "phone_country_code",
    }

    # Additional fields present only when the Desktop app provides them
    DOMHAND_OPTIONAL_FIELDS: ClassVar[set[str]] = {
        "linkedin",
        "zip",
        "postal_code",
        "work_authorization",
        "visa_sponsorship",
        "veteran_status",
        "disability_status",
        "gender",
        "race_ethnicity",
    }

    DOMHAND_ALL_FIELDS: ClassVar[set[str]] = DOMHAND_GUARANTEED_FIELDS | DOMHAND_OPTIONAL_FIELDS

    def test_full_profile_has_all_domhand_fields(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        # Full profile provides linkedIn and zipCode, so ALL fields should exist
        missing = self.DOMHAND_ALL_FIELDS - set(result.keys())
        assert not missing, f"Missing DomHand-expected fields: {missing}"

    def test_full_profile_field_values_are_non_empty(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        for field in self.DOMHAND_ALL_FIELDS:
            val = result.get(field)
            assert val is not None, f"Field {field!r} is None"
            assert val != "", f"Field {field!r} is empty string"

    def test_minimal_profile_has_all_guaranteed_fields(self):
        """Even a minimal profile should have all guaranteed fields after
        the pipeline fills in structural defaults. Optional fields only appear
        when the Desktop app provides them."""
        minimal: dict[str, Any] = {
            "firstName": "Min",
            "lastName": "Test",
            "email": "min@test.com",
            "phone": "+10000000000",
        }
        result = _full_pipeline(minimal)

        missing = self.DOMHAND_GUARANTEED_FIELDS - set(result.keys())
        assert not missing, f"Missing DomHand-guaranteed fields: {missing}"

        # Optional fields should NOT be present if not provided
        assert "linkedin" not in result
        assert "zip" not in result
        assert "work_authorization" not in result
        assert "gender" not in result

    def test_education_entries_have_snake_case_keys(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        for edu in result.get("education", []):
            if "fieldOfStudy" in edu:
                assert "field_of_study" in edu
            if "graduationDate" in edu:
                assert "graduation_date" in edu

    def test_experience_entries_have_snake_case_keys(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        for exp in result.get("experience", []):
            if "startDate" in exp:
                assert "start_date" in exp
            if "endDate" in exp:
                assert "end_date" in exp

    def test_postal_code_present_when_zipcode_sent(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        assert "postal_code" in result
        assert result["postal_code"] == result["zip"]

    def test_address_dict_present(self):
        result = _full_pipeline(REAL_DESKTOP_PROFILE)

        assert "address" in result
        if isinstance(result["address"], dict):
            assert "country" in result["address"]
