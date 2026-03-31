"""LLM batch answer generation, JSON parsing, and answer resolution.

Dependencies that still live in ``domhand_fill`` or sibling ``dom.*`` modules are
accessed via late imports to avoid circular references.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, create_model

from ghosthands.actions.views import (
    FormField,
    is_placeholder_value,
    normalize_name,
)

if TYPE_CHECKING:
    from ghosthands.actions.domhand_fill import ResolvedFieldValue

logger = structlog.get_logger(__name__)


class _DomHandAnswerBatchBase(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ── Late-import delegates ────────────────────────────────────────────────


def _is_skill_like(name: str) -> bool:
    from ghosthands.actions.domhand_fill import _is_skill_like as _impl

    return _impl(name)


def _field_label_candidates(field: FormField) -> list[str]:
    from ghosthands.actions.domhand_fill import _field_label_candidates as _impl

    return _impl(field)


def _is_non_guess_name_fragment(label: str) -> bool:
    from ghosthands.actions.domhand_fill import _is_non_guess_name_fragment as _impl

    return _impl(label)


def _normalize_match_label(text: str) -> str:
    from ghosthands.dom.fill_label_match import _normalize_match_label as _impl

    return _impl(text)


def _label_match_confidence(label: str, key: str) -> str | None:
    from ghosthands.dom.fill_label_match import _label_match_confidence as _impl

    return _impl(label, key)


def _meets_match_confidence(confidence: str | None, minimum: str) -> bool:
    from ghosthands.dom.fill_label_match import _meets_match_confidence as _impl

    return _impl(confidence, minimum)


def _coerce_answer_if_compatible(field: FormField, raw: Any, *, source_candidate: str) -> str | None:
    from ghosthands.dom.fill_profile_resolver import _coerce_answer_if_compatible as _impl

    return _impl(field, raw, source_candidate=source_candidate)


def _coerce_answer_to_field(field: FormField, answer: str | None) -> str | None:
    from ghosthands.dom.fill_label_match import _coerce_answer_to_field as _impl

    return _impl(field, answer)


def _resolved_field_value(
    value: str, *, source: str, answer_mode: str | None, confidence: float, state: str = "filled"
) -> ResolvedFieldValue:
    from ghosthands.dom.fill_profile_resolver import _resolved_field_value as _impl

    return _impl(value, source=source, answer_mode=answer_mode, confidence=confidence, state=state)


def _resolved_field_value_if_compatible(
    field: FormField, value: str, *, source: str, source_candidate: str, answer_mode: str | None, confidence: float
) -> ResolvedFieldValue | None:
    from ghosthands.dom.fill_profile_resolver import _resolved_field_value_if_compatible as _impl

    return _impl(
        field, value, source=source, source_candidate=source_candidate, answer_mode=answer_mode, confidence=confidence
    )


def _match_confidence_score(confidence: str | None) -> float:
    from ghosthands.dom.fill_profile_resolver import _match_confidence_score as _impl

    return _impl(confidence)


def _default_answer_mode_for_field(field: FormField, value: str) -> str:
    from ghosthands.dom.fill_profile_resolver import _default_answer_mode_for_field as _impl

    return _impl(field, value)


def _default_value(field: FormField) -> str | None:
    from ghosthands.dom.fill_profile_resolver import _default_value as _impl

    return _impl(field)


def _known_profile_value(field_name: str, evidence: dict[str, str | None]) -> str | None:
    from ghosthands.dom.fill_profile_resolver import _known_profile_value as _impl

    return _impl(field_name, evidence)


def _parse_profile_evidence(profile_text: str) -> dict[str, str | None]:
    from ghosthands.dom.fill_profile_resolver import _parse_profile_evidence as _impl

    return _impl(profile_text)


def _preferred_field_label(field: FormField) -> str:
    from ghosthands.dom.fill_label_match import _preferred_field_label as _impl

    return _impl(field)


def _trace_profile_resolution(event: str, **kwargs: Any) -> None:
    from ghosthands.dom.fill_profile_resolver import _trace_profile_resolution as _impl

    return _impl(event, **kwargs)


def _profile_debug_preview(value: Any) -> str:
    from ghosthands.dom.fill_profile_resolver import _profile_debug_preview as _impl

    return _impl(value)


def _is_binary_value_text(text: str) -> bool:
    from ghosthands.dom.fill_profile_resolver import _is_binary_value_text as _impl

    return _impl(text)


def _build_field_description(field: FormField, display_name: str) -> str:
    from ghosthands.actions.domhand_fill import _build_field_description as _impl

    return _impl(field, display_name)


def _replace_placeholder_answers(parsed: dict, fields: list, names: list) -> None:
    from ghosthands.actions.domhand_fill import _replace_placeholder_answers as _impl

    return _impl(parsed, fields, names)


def _match_answer(field: FormField, answers: dict, evidence: dict, profile_data: dict | None) -> str | None:
    from ghosthands.actions.domhand_fill import _match_answer as _impl

    return _impl(field, answers, evidence, profile_data)


def _get_profile_text() -> str | None:
    from ghosthands.actions.domhand_fill import _get_profile_text as _impl

    return _impl()


def _MATCH_CONFIDENCE_RANKS_getter() -> dict[str, int]:
    from ghosthands.dom.fill_label_match import _MATCH_CONFIDENCE_RANKS

    return _MATCH_CONFIDENCE_RANKS


def _AUTHORITATIVE_SELECT_KEYS_getter() -> dict[str, list[str]]:
    from ghosthands.actions.domhand_fill import _AUTHORITATIVE_SELECT_KEYS

    return _AUTHORITATIVE_SELECT_KEYS


def _AUTHORITATIVE_SELECT_DEFAULTS_getter() -> dict[str, str]:
    from ghosthands.actions.domhand_fill import _AUTHORITATIVE_SELECT_DEFAULTS

    return _AUTHORITATIVE_SELECT_DEFAULTS


def _AUTHORITATIVE_TEXT_DEFAULTS_getter() -> dict[str, str]:
    from ghosthands.actions.domhand_fill import _AUTHORITATIVE_TEXT_DEFAULTS

    return _AUTHORITATIVE_TEXT_DEFAULTS


def _EEO_DECLINE_DEFAULTS_getter() -> dict[str, str]:
    from ghosthands.actions.domhand_fill import _EEO_DECLINE_DEFAULTS

    return _EEO_DECLINE_DEFAULTS


def _SOCIAL_OR_ID_NO_GUESS_RE_getter():
    from ghosthands.actions.domhand_fill import _SOCIAL_OR_ID_NO_GUESS_RE

    return _SOCIAL_OR_ID_NO_GUESS_RE


# ── Extracted functions ──────────────────────────────────────────────────


def _resolve_llm_answer_for_field(
    field: FormField,
    answers: dict[str, str],
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
) -> ResolvedFieldValue | None:
    if _is_skill_like(field.name):
        return None
    label_candidates = _field_label_candidates(field) or [field.name]
    if any(_is_non_guess_name_fragment(label) for label in label_candidates):
        return None
    candidate_norms = [_normalize_match_label(label) for label in label_candidates if _normalize_match_label(label)]
    # Button-groups with explicit choices are constrained (answer must be one of the
    # listed options), so use "medium" confidence even for optional fields.
    _has_explicit_choices = bool(field.choices or field.options) and field.field_type in {
        "button-group", "radio-group", "radio", "checkbox-group",
    }
    minimum_confidence = "medium" if (field.required or _has_explicit_choices) else "strong"

    _ASK = _AUTHORITATIVE_SELECT_KEYS_getter()
    _ASD = _AUTHORITATIVE_SELECT_DEFAULTS_getter()
    _ATD = _AUTHORITATIVE_TEXT_DEFAULTS_getter()
    _EDD = _EEO_DECLINE_DEFAULTS_getter()
    _MCR = _MATCH_CONFIDENCE_RANKS_getter()

    if field.field_type == "select":
        for norm_name in candidate_norms:
            if norm_name in _ASK:
                for ck in _ASK[norm_name]:
                    if ck in answers:
                        coerced = _coerce_answer_if_compatible(
                            field,
                            answers[ck],
                            source_candidate="llm",
                        )
                        if coerced:
                            return _resolved_field_value(
                                coerced,
                                source="llm",
                                answer_mode="best_effort_guess",
                                confidence=0.64,
                            )
                    for key, val in answers.items():
                        if normalize_name(key) == normalize_name(ck):
                            coerced = _coerce_answer_if_compatible(
                                field,
                                val,
                                source_candidate="llm",
                            )
                            if coerced:
                                return _resolved_field_value(
                                    coerced,
                                    source="llm",
                                    answer_mode="best_effort_guess",
                                    confidence=0.6,
                                )
                if norm_name in _ASD:
                    default_value = _ASD[norm_name]
                    return _resolved_field_value_if_compatible(
                        field,
                        default_value,
                        source="dom",
                        source_candidate="default",
                        answer_mode=_default_answer_mode_for_field(field, default_value),
                        confidence=0.66,
                    )

    best_resolution: ResolvedFieldValue | None = None
    best_rank = 0
    for key, val in answers.items():
        for candidate in label_candidates:
            confidence = _label_match_confidence(candidate, key)
            if not _meets_match_confidence(confidence, minimum_confidence):
                continue
            rank = _MCR.get(confidence or "", 0)
            if rank <= best_rank:
                continue
            coerced = _coerce_answer_if_compatible(
                field,
                val,
                source_candidate="llm",
            )
            if not coerced:
                continue
            best_rank = rank
            best_resolution = _resolved_field_value(
                coerced,
                source="llm",
                answer_mode="best_effort_guess",
                confidence=_match_confidence_score(confidence),
            )
            if rank == _MCR["exact"]:
                return best_resolution

    if best_resolution is not None:
        return best_resolution

    for norm_name in candidate_norms:
        if norm_name in _ASD:
            default_value = _ASD[norm_name]
            return _resolved_field_value_if_compatible(
                field,
                default_value,
                source="dom",
                source_candidate="default",
                answer_mode=_default_answer_mode_for_field(field, default_value),
                confidence=0.66,
            )

    if field.field_type in {"text", "textarea", "search"}:
        for norm_name in candidate_norms:
            if norm_name in _ATD:
                default_value = _ATD[norm_name]
                return _resolved_field_value_if_compatible(
                    field,
                    default_value,
                    source="dom",
                    source_candidate="default",
                    answer_mode=_default_answer_mode_for_field(field, default_value),
                    confidence=0.66,
                )

    if field.required:
        for norm_name in candidate_norms:
            if norm_name in _EDD:
                default_value = _EDD[norm_name]
                return _resolved_field_value_if_compatible(
                    field,
                    default_value,
                    source="dom",
                    source_candidate="default",
                    answer_mode="default_decline",
                    confidence=0.72,
                )

    default_value = _default_value(field)
    if not default_value:
        return None
    return _resolved_field_value_if_compatible(
        field,
        default_value,
        source="dom",
        source_candidate="default",
        answer_mode=_default_answer_mode_for_field(field, default_value),
        confidence=0.65,
    )


def _is_explicit_false(val: str | None) -> bool:
    """Return True if the value explicitly indicates unchecked/off/no."""
    if not val:
        return False
    return bool(re.match(r"^(unchecked|false|no|off|0)$", val.strip(), re.IGNORECASE))


def _takeover_suggestion_for_field(field: FormField, success: bool, actor: str, error: str | None) -> str | None:
    """Return a high-level takeover hint for browser-use after DomHand acts."""
    if success:
        return None
    if actor == "skipped":
        return "leave_blank" if not field.required else "browser_use_takeover"
    if field.field_type in {"select", "radio-group", "checkbox-group", "button-group", "radio", "checkbox", "toggle"}:
        return "browser_use_takeover"
    if field.field_type in {"text", "email", "tel", "url", "number", "password", "search", "date", "textarea"}:
        return "browser_use_takeover" if field.required else "retry_with_commit"
    if error and "REQUIRED" in error:
        return "browser_use_takeover"
    return "browser_use_takeover"


# ── LLM answer generation ───────────────────────────────────────────


def _sanitize_no_guess_answer(
    field_name: str,
    required: bool,
    answer: str | None,
    evidence: dict[str, str | None],
    *,
    field_type: str = "",
    question_text: str = "",
) -> str:
    """Prevent fabrication of sensitive identity fields not in profile.

    Apply flows do not pause for HITL. If the model still emits the legacy
    ``[NEEDS_USER_INPUT]`` marker, suppress it and fall back to saved/best-effort
    answers instead.
    """
    proposed = (answer or "").strip()
    known = _known_profile_value(field_name, evidence)
    if _is_non_guess_name_fragment(question_text or field_name):
        return known or ""

    if proposed and "[NEEDS_USER_INPUT]" in proposed.upper():
        if known:
            _trace_profile_resolution(
                "domhand.profile_needs_input_overridden",
                field_label=field_name,
                proposed_marker="[NEEDS_USER_INPUT]",
                recovered_value=_profile_debug_preview(known),
            )
            return known
        best_effort = _known_profile_value(question_text or field_name, evidence)
        if best_effort:
            _trace_profile_resolution(
                "domhand.profile_needs_input_best_effort",
                field_label=field_name,
                proposed_marker="[NEEDS_USER_INPUT]",
                recovered_value=_profile_debug_preview(best_effort),
            )
            return best_effort
        _trace_profile_resolution(
            "domhand.profile_needs_input_suppressed",
            field_label=field_name,
            proposed_marker="[NEEDS_USER_INPUT]",
            recovered_value="EMPTY",
        )
        return ""

    if known:
        return known
    if not _SOCIAL_OR_ID_NO_GUESS_RE_getter().search(field_name or ""):
        return proposed
    if not proposed:
        return ""
    if is_placeholder_value(proposed) or re.match(
        r"^(n/a|na|none|unknown|not applicable|prefer not|decline)", proposed, re.IGNORECASE
    ):
        return ""
    if field_type == "select" and _is_binary_value_text(proposed):
        return proposed
    return ""


def _disambiguated_field_names(fields: list[FormField]) -> list[str]:
    """Build deterministic display names for batched LLM answer generation.

    Newlines and control characters in labels are replaced with spaces so the
    LLM can echo them back as valid JSON keys without parse errors.
    """
    name_counts: dict[str, int] = {}
    disambiguated_names: list[str] = []
    for i, field in enumerate(fields):
        base_name = _preferred_field_label(field) or f"Field {i + 1}"
        # Sanitize control characters that break JSON when echoed as keys
        base_name = re.sub(r"[\x00-\x1f\x7f]+", " ", base_name).strip()
        norm = normalize_name(base_name) or f"field-{i + 1}"
        count = name_counts.get(norm, 0) + 1
        name_counts[norm] = count
        disambiguated_names.append(f"{base_name} #{count}" if count > 1 else base_name)
    return disambiguated_names


def _repair_invalid_json_string_escapes(blob: str) -> str:
    """Repair invalid escapes and control characters inside JSON strings.

    Models often emit literal newlines or bad backslash escapes inside JSON
    string values (e.g. field labels containing \\n\\n).  This repairs them
    so ``json.loads`` succeeds.
    """
    ctrl_map = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out: list[str] = []
    i = 0
    in_str = False
    while i < len(blob):
        c = blob[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
            i += 1
            continue
        # Escape raw control characters inside JSON strings
        if c in ctrl_map:
            out.append(ctrl_map[c])
            i += 1
            continue
        if ord(c) < 0x20 and c not in ctrl_map:
            out.append(f"\\u{ord(c):04x}")
            i += 1
            continue
        if c == "\\" and i + 1 < len(blob):
            nxt = blob[i + 1]
            if nxt in '\\"/bfnrt':
                out.extend((c, nxt))
                i += 2
                continue
            if nxt == "u" and i + 6 <= len(blob):
                hx = blob[i + 2 : i + 6]
                if len(hx) == 4 and all(h in "0123456789abcdefABCDEF" for h in hx):
                    out.append(blob[i : i + 6])
                    i += 6
                    continue
            i += 1
            if i < len(blob):
                out.append(blob[i])
                i += 1
            continue
        if c == '"':
            in_str = False
        out.append(c)
        i += 1
    return "".join(out)


def _parse_llm_json_answer_object(text: str) -> dict[str, Any]:
    """Parse DomHand batch JSON: strip fences, skip prefix junk, tolerate bad string escapes."""
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE).strip()
    blobs: list[str] = [cleaned]
    repaired = _repair_invalid_json_string_escapes(cleaned)
    if repaired != cleaned:
        blobs.append(repaired)
    decoder = json.JSONDecoder()
    last_err: json.JSONDecodeError | None = None
    for blob in blobs:
        start = blob.find("{")
        if start < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(blob, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            last_err = e
    if last_err is not None:
        raise last_err
    raise json.JSONDecodeError("No JSON object in model response", cleaned, 0)


def _resolve_llm_answer_via_batch_key(
    field: FormField,
    batch_key: str,
    answers: dict[str, str],
) -> ResolvedFieldValue | None:
    """When the model used the batch key (e.g. Field 1), map directly."""
    if _is_skill_like(field.name):
        return None
    if batch_key not in answers:
        return None
    raw = answers[batch_key]
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    coerced = _coerce_answer_if_compatible(
        field,
        raw,
        source_candidate="llm",
    )
    if not coerced:
        return None
    return _resolved_field_value(
        coerced,
        source="llm",
        answer_mode="best_effort_guess",
        confidence=0.92,
    )


def _build_answer_output_model(display_names: list[str]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for index, display_name in enumerate(display_names):
        fields[f"field_{index}"] = (
            str | list[str] | None,
            Field(default=None, alias=display_name, serialization_alias=display_name),
        )
    return cast(
        type[BaseModel],
        create_model(
            "DomHandAnswerBatch",
            __base__=_DomHandAnswerBatchBase,
            **fields,
        ),
    )


def _structured_completion_to_answer_map(completion: Any) -> dict[str, Any] | None:
    if isinstance(completion, BaseModel):
        return completion.model_dump(by_alias=True, exclude_none=True)
    if isinstance(completion, dict):
        return completion
    if isinstance(completion, str):
        return _parse_llm_json_answer_object(completion)
    return None


def _normalize_generated_answer_map(parsed: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in parsed.items():
        if value is None:
            continue
        if isinstance(value, list):
            normalized[key] = ",".join(str(item) for item in value)
        elif isinstance(value, (int, float)):
            normalized[key] = str(value)
        else:
            normalized[key] = str(value)
    return normalized


async def _generate_answers(
    fields: list[FormField],
    profile_text: str,
    profile_data: dict[str, Any] | None = None,
) -> tuple[dict[str, str], int, int, float, str | None]:
    """Call the configured DomHand model to generate answers for all fields in a single batch."""
    try:
        from browser_use.llm.messages import UserMessage
        from ghosthands.config.models import estimate_cost
        from ghosthands.config.settings import settings as _settings
        from ghosthands.llm.client import get_chat_model
    except ImportError:
        logger.error("ghosthands.llm.client not available — cannot generate answers")
        return {}, 0, 0, 0.0, None

    evidence = _parse_profile_evidence(profile_text)
    model_id = _settings.domhand_model
    llm = get_chat_model(model=model_id, disable_google_thinking=True)
    model_id = getattr(llm, "model", model_id)  # resolved model after proxy override
    input_tokens = 0
    output_tokens = 0
    step_cost = 0.0

    disambiguated_names = _disambiguated_field_names(fields)
    output_model = _build_answer_output_model(disambiguated_names)

    field_descriptions = "\n".join(
        _build_field_description(field, disambiguated_names[i]) for i, field in enumerate(fields)
    )

    today = date.today().isoformat()
    prompt = f"""You are filling out a job application form on behalf of an applicant. Today's date is {today}.

Here is their profile:

{profile_text}

Here are the form fields to fill:

{field_descriptions}

Rules:
- For each field, decide what value to put based on the profile.
- You are helping a job seeker complete applications successfully. Within the rules below, prefer answers that keep the candidate eligible and aligned with employer requirements.
- For substantive applicant fields, use ONLY the applicant's actual profile data. Do not invent salary, start dates, work history, education, essays, addresses, or personal identifiers.
- If the profile has NO relevant data for an OPTIONAL field, return "" (empty string). NEVER make up data or use placeholder values like "N/A", "None", "Not applicable", etc.
- If the profile has NO direct answer for a REQUIRED field: first use a saved survey/profile answer, then a semantically equivalent QA-bank answer, then the closest structured education/experience value, then a deterministic default when one exists, and finally your best-effort guess. NEVER return "[NEEDS_USER_INPUT]".
- NEVER fabricate personal identifiers or social handles/URLs not explicitly in the profile. If missing: return "" (empty string) for optional fields, and omit the field if you still cannot infer a safe answer.
- For dropdowns/radio groups/button-groups with listed options, pick the EXACT text of one of the available options.
- For hierarchical dropdown options (format "Category > SubOption"), pick the EXACT full path including the " > " separator.
- Use the section, current field value, sibling field names, and listed options together to infer meaning for short or generic labels.
- If a label is generic (for example "Overall", "Type", "Status", or "Source"), do NOT rely on label matching alone. Use section context plus the available options to choose the best answer.
- For dropdowns WITHOUT listed options, provide the value from the profile if available. If the field name closely matches a profile entry, use that value.
- For School/University/Institution search dropdowns, ALWAYS use the FULL official name (e.g., "University of California, Los Angeles" not "UCLA"; "Massachusetts Institute of Technology" not "MIT"; "New York University" not "NYU"). The dropdown searches by full name, not abbreviations.
- For low-risk standardized screening fields, prefer a best-effort answer instead of "[NEEDS_USER_INPUT]". This includes referral/source fields, phone type, country/country of residence, work arrangement, relocation, and demographic/EEO fields.
- For low-risk standardized screening dropdowns/radios, use the saved profile/default answer and choose the closest matching option text if the wording differs slightly.
- For "How did you hear about us?" or similar source/referral fields: use the applicant profile value if available. If the profile has no source, default to "LinkedIn" (or the closest matching option like "Job Board", "Online Job Board", "Internet"). NEVER return "[NEEDS_USER_INPUT]" for referral source fields — they always have a safe default.
- For "Phone Device Type" or similar phone type fields: default to "Mobile" if the profile has no phone type. NEVER return "[NEEDS_USER_INPUT]" for phone type fields.
- For the main "Phone" / "Phone number" / "<tel>" field: put the applicant's actual phone number digits (from the profile). NEVER use "Mobile", "Home", "Work", or "Cell" there — those belong only on explicit phone *type* / *device* questions, not the number box.
- For structured education fields (GPA, field of study, actual vs expected end date, scoped education start/end dates) and structured language fields ("Language", fluent, reading/writing, speaking/listening, overall proficiency), NEVER guess. Only answer when the exact structured profile value is present.
- When the profile wording and the site wording differ, map to the semantically closest visible option instead of escalating. Example: "Native / bilingual" may map to the top proficiency tier such as "Expert", "Advanced", or "Fluent".
- For work-setup/location preference fields, use the saved preferred work mode / preferred locations if present. If the field is broad and only asks for an arrangement, prefer the work-mode answer over escalating.
- For relocation fields, use the applicant's saved relocation preference. If the profile does not specify one, default to "Anywhere". For generic willingness-to-relocate questions and location-specific office-attendance questions, prefer the employer-compatible answer unless the profile explicitly says the applicant cannot relocate or cannot be in-office at that location.
- For upload-like controls rendered as buttons (for example "Attach", "Upload", "Browse", "Choose file", "Enter manually"), skip them just like file-upload fields.
- For skill typeahead fields, return an ARRAY of relevant skills from the applicant profile.
- For multi-select fields, return a JSON array of ALL matching options (e.g., ["Python", "Java"]).
- For checkboxes/toggles, respond with "checked" or "unchecked".
- IMPORTANT: For agreement/consent checkboxes (e.g., "I agree", "I accept", "I understand", privacy policy, terms of service, candidate consent), ALWAYS respond with "checked". The applicant consents to standard application agreements.
- IMPORTANT: For Yes/No questions about government, regulatory, criminal history, convictions, felonies, sanctions, debarment, or political affiliation, ALWAYS answer "No" unless the applicant profile explicitly states otherwise. Do NOT infer government experience from an employer name (e.g., NASA, DOD contractors, research labs). These questions ask about direct government/regulatory employment, not tangential associations.
- For file upload fields, skip them (don't include in output).
- For textarea fields, use an explicit open-ended answer from the applicant profile when available. If the profile does not contain that answer, use a semantically matched survey/profile value, structured education/experience fallback, deterministic default, or best-effort guess for required fields.
- For demographic/EEO fields (gender, race, ethnicity, veteran status, disability status, sexual orientation): give exactly ONE value — these are single-answer fields even if the widget allows multiple selections. Use the applicant's actual info if provided. If no info is provided in the profile, use a neutral decline option: "I decline to self-identify", "I am not a protected veteran", or "I do not wish to answer" (pick whichever matches the available options). NEVER combine multiple values like "Man, I don't wish to answer". Pick ONE. NEVER return "[NEEDS_USER_INPUT]" for EEO fields — always use a decline default.
- NEVER select a default placeholder value like "Select One", "Please select", etc.
- NEVER use placeholder strings like "N/A", "NA", "Not applicable", or "Unknown". Use the literal string "None" only when the question is asking about certifications/licenses and the applicant has none.
- For salary or compensation fields, use the saved salary expectation first. If the profile does not contain one, make a best-effort guess instead of returning "[NEEDS_USER_INPUT]".
- Use the EXACT field names shown above (including any "#N" suffix) as JSON keys.
- Only include fields you have a real answer for. For REQUIRED fields you must provide your best answer and never emit "[NEEDS_USER_INPUT]".
- Respond with ONLY a valid JSON object. No explanation, no markdown fences.
- Inside JSON strings use valid JSON only: for apostrophes use a straight quote ' with NO backslash. Never write backslash-space (e.g. WHOOP\\ s) — that breaks JSON.

Example: {{"First Name": "Alex", "Cover Letter": "I am excited to apply because..."}}"""

    scaled_max_tokens = max(4096, min(len(fields) * 128, 16384))
    messages = [UserMessage(content=prompt)]
    try:
        try:
            response = await llm.ainvoke(
                messages,
                output_format=output_model,
                max_tokens=scaled_max_tokens,
            )
            parsed = _structured_completion_to_answer_map(response.completion)
            if parsed is None:
                raise ValueError("Structured answer completion could not be converted to an answer map")
        except Exception as structured_error:
            logger.warning(
                "domhand.structured_answer_fallback",
                error=str(structured_error),
                model=model_id,
            )
            response = await llm.ainvoke(
                messages,
                max_tokens=scaled_max_tokens,
            )
            text = response.completion if isinstance(response.completion, str) else ""
            logger.warning(f"LLM answer response: {text[:500]}{'...' if len(text) > 500 else ''}")
            parsed = _parse_llm_json_answer_object(text)

        input_tokens = response.usage.prompt_tokens if response.usage else 0
        output_tokens = response.usage.completion_tokens if response.usage else 0
        try:
            step_cost = estimate_cost(model_id, input_tokens, output_tokens)
        except Exception as e:
            logger.warning(f'Failed to estimate LLM cost for model "{model_id}": {e}')
            step_cost = 0.0
        if response.stop_reason == "max_tokens":
            logger.warning("LLM response was truncated (hit max_tokens).")

        parsed = _normalize_generated_answer_map(parsed)

        _replace_placeholder_answers(parsed, fields, disambiguated_names)

        pre_sanitize_keys = set(parsed.keys())
        for i, field in enumerate(fields):
            key = disambiguated_names[i]
            if key in parsed and isinstance(parsed[key], str):
                parsed[key] = _sanitize_no_guess_answer(
                    field.name,
                    field.required,
                    parsed[key],
                    evidence,
                    field_type=field.field_type,
                    question_text=field.raw_label or field.name,
                )

        empty_after_sanitize = [k for k, v in parsed.items() if not v]
        missing_keys = [disambiguated_names[i] for i in range(len(fields)) if disambiguated_names[i] not in parsed]
        if empty_after_sanitize or missing_keys:
            logger.warning(
                "domhand.llm_answer_quality",
                total_fields=len(fields),
                llm_returned_keys=len(pre_sanitize_keys),
                empty_after_sanitize=empty_after_sanitize[:10],
                missing_from_llm=missing_keys[:10],
            )

        return parsed, input_tokens, output_tokens, step_cost, model_id
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON, using empty answers")
        return {}, input_tokens, output_tokens, step_cost, model_id
    except Exception as e:
        logger.error(f"LLM answer generation failed: {e}")
        return {}, input_tokens, output_tokens, step_cost, model_id


async def infer_answers_for_fields(
    fields: list[FormField],
    *,
    profile_text: str | None = None,
    profile_data: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Infer answers for an arbitrary field set using DomHand's option-aware LLM path."""
    if not fields:
        return {}

    effective_profile_text = profile_text or _get_profile_text() or ""
    if not effective_profile_text and profile_data:
        effective_profile_text = json.dumps(profile_data)
    if not effective_profile_text:
        return {}

    answers, *_ = await _generate_answers(
        fields,
        effective_profile_text,
        profile_data=profile_data,
    )
    evidence = _parse_profile_evidence(effective_profile_text)
    display_names = _disambiguated_field_names(fields)
    resolved: dict[str, str] = {}

    for field, display_name in zip(fields, display_names, strict=False):
        proposed = answers.get(display_name)
        if proposed is None:
            proposed = _match_answer(field, answers, evidence, profile_data)
        coerced = _coerce_answer_to_field(field, str(proposed).strip() if proposed is not None else None)
        if not coerced or "[NEEDS_USER_INPUT]" in coerced.upper():
            continue
        resolved[field.field_id] = coerced

    return resolved
