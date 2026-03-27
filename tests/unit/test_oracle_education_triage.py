"""Oracle HCM education triage: school/field comboboxes skip dangerous dropdown coercion."""

from ghosthands.actions.domhand_fill import (
    StructuredRepeaterDiagnostic,
    _structured_education_oracle_combobox_skip_dropdown_coercion,
)
from ghosthands.actions.views import FormField
from ghosthands.dom.fill_label_match import _coerce_answer_to_field


def test_skip_true_for_non_native_school_select():
    f = FormField(
        field_id="s1",
        name="School",
        field_type="select",
        is_native=False,
        options=["9 Eylul University"],
    )
    diag = StructuredRepeaterDiagnostic(
        repeater_group="education",
        field_id="s1",
        field_label="School",
        section="Education 1",
        slot_name="school",
        numeric_index=0,
        current_value="",
    )
    assert _structured_education_oracle_combobox_skip_dropdown_coercion(
        f, is_structured_education_candidate=True, structured_education_diag=diag
    )


def test_skip_true_for_field_of_study_non_native_select():
    f = FormField(field_id="m1", name="Field of Study", field_type="select", is_native=False)
    diag = StructuredRepeaterDiagnostic(
        repeater_group="education",
        field_id="m1",
        field_label="Field of Study",
        section="Education 1",
        slot_name="field_of_study",
        numeric_index=0,
        current_value="",
    )
    assert _structured_education_oracle_combobox_skip_dropdown_coercion(
        f, is_structured_education_candidate=True, structured_education_diag=diag
    )


def test_skip_false_for_native_school_select():
    f = FormField(field_id="s1", name="School", field_type="select", is_native=True)
    diag = StructuredRepeaterDiagnostic(
        repeater_group="education",
        field_id="s1",
        field_label="School",
        section="Education 1",
        slot_name="school",
        numeric_index=0,
        current_value="",
    )
    assert not _structured_education_oracle_combobox_skip_dropdown_coercion(
        f, is_structured_education_candidate=True, structured_education_diag=diag
    )


def test_skip_false_for_school_country_slot():
    f = FormField(field_id="c1", name="Country", field_type="select", is_native=False)
    diag = StructuredRepeaterDiagnostic(
        repeater_group="education",
        field_id="c1",
        field_label="Country",
        section="Education 1",
        slot_name="school_country",
        numeric_index=0,
        current_value="",
    )
    assert not _structured_education_oracle_combobox_skip_dropdown_coercion(
        f, is_structured_education_candidate=True, structured_education_diag=diag
    )


def test_coerce_school_maps_ucla_to_first_university_overlap():
    """Layer-1 failure mode: alphabetical slice + word overlap picks the wrong school."""
    options = [
        "9 Eylul University",
        "A B Freeman School of Business",
        "Aarhus University",
        "Abant Izzet Baysal University",
        "Abbottabad University of Science and Technology",
    ]
    f = FormField(
        field_id="sch",
        name="School",
        field_type="select",
        is_native=False,
        options=options,
    )
    coerced = _coerce_answer_to_field(f, "University of California, Los Angeles")
    assert coerced is not None
    assert "Eylul" in coerced, (
        "Expected word-overlap onto a wrong 'University*' option; triage must bypass coercion for Oracle school."
    )
