"""GPT-driven Oracle school combobox: search phrases and option picking.

Executor types into the combobox, lists visible rows in Python, and asks a small
OpenAI chat model to choose an index or reject — no Python string-similarity for
the final pick.

When ``GH_LLM_PROXY_URL`` is set, requests use the VALET managed-inference path
``{proxy}/inference/openai/v1`` (same pattern as ``.../inference/gemini/...``),
with the runtime grant on the ``x-api-key`` header.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator

from ghosthands.config.settings import settings

logger = structlog.get_logger(__name__)

ORACLE_SCHOOL_OPENAI_MODEL = "gpt-5.4-nano"

_MAX_TERMS_PER_RESPONSE = 8
_MAX_OPTION_LINES = 30


class _SearchTermsPayload(BaseModel):
    terms: list[str] = Field(default_factory=list)

    @field_validator("terms", mode="before")
    @classmethod
    def _strip_terms(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            t = str(item).strip()
            if t and len(t) <= 120:
                out.append(t)
        return out[:_MAX_TERMS_PER_RESPONSE]


class _PickPayload(BaseModel):
    matched_index: int | None = None

    @field_validator("matched_index", mode="before")
    @classmethod
    def _coerce_index(cls, v: Any) -> int | None:
        if v is None or v == "null":
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v if v >= 0 else None
        if isinstance(v, float):
            i = int(v)
            return i if i >= 0 else None
        try:
            i = int(str(v).strip())
            return i if i >= 0 else None
        except (TypeError, ValueError):
            return None


class _VerifyPayload(BaseModel):
    same_institution: bool = False


def _oracle_school_openai_base_url() -> str | None:
    raw = (settings.llm_proxy_url or "").strip().rstrip("/")
    if not raw:
        return None
    return f"{raw}/inference/openai/v1"


def _oracle_school_llm_disabled() -> bool:
    """True when we cannot call OpenAI (direct or via VALET)."""
    if settings.llm_proxy_url:
        return not (settings.llm_runtime_grant or settings.openai_api_key)
    return not settings.openai_api_key


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```\s*$", "", raw)
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def _oracle_school_chat_openai(*, max_completion_tokens: int) -> Any:
    from browser_use.llm.openai.chat import ChatOpenAI

    base = _oracle_school_openai_base_url()
    grant = (settings.llm_runtime_grant or "").strip()
    direct_key = (settings.openai_api_key or "").strip()

    default_headers: dict[str, str] | None = None
    if settings.llm_proxy_url and grant:
        default_headers = {"x-api-key": grant}

    api_key = grant or direct_key or "dummy-unset-key"

    return ChatOpenAI(
        model=ORACLE_SCHOOL_OPENAI_MODEL,
        temperature=0.0,
        max_completion_tokens=max_completion_tokens,
        api_key=api_key,
        base_url=base,
        default_headers=default_headers,
    )


async def _completion_text(user_prompt: str, *, max_tokens: int = 512) -> str:
    from browser_use.llm.messages import UserMessage

    llm = _oracle_school_chat_openai(max_completion_tokens=max_tokens)
    out = await llm.ainvoke([UserMessage(content=user_prompt)])
    completion = getattr(out, "completion", None)
    if isinstance(completion, str):
        return completion.strip()
    return str(completion or "").strip()


async def oracle_combobox_search_terms_llm(
    canonical_school: str,
    *,
    prior_terms_tried: list[str] | None = None,
) -> list[str]:
    """Return ordered typeahead phrases. Empty if LLM unavailable or parse fails."""
    school = " ".join(str(canonical_school or "").split()).strip()
    if not school or _oracle_school_llm_disabled():
        return []

    extra = ""
    if prior_terms_tried:
        preview = "; ".join(prior_terms_tried[:12])
        extra = f"\nThese search phrases already failed to surface a clear match (do not repeat them): {preview}\n"

    prompt = f"""You help a browser automation tool fill an Oracle HCM school combobox.
The applicant's institution (canonical) is:
\"\"\"{school}\"\"\"{extra}

Reply with ONLY a JSON object (no markdown) in this exact shape:
{{"terms": ["phrase1", "phrase2", ...]}}

Rules:
- "terms" is an ordered list of short strings to TYPE into the search box (max {_MAX_TERMS_PER_RESPONSE} items).
- Prefer distinctive leading fragments first (e.g. "University of California" before "Los Angeles"), then city/campus, then well-known abbreviations if obvious.
- Do not include full prose or explanations.
- Use English unless the canonical name is clearly in another language.
"""
    try:
        text = await _completion_text(prompt, max_tokens=512)
        data = _extract_json_object(text)
        payload = _SearchTermsPayload.model_validate(data)
        logger.debug(
            "domhand.oracle_school_llm_terms",
            term_count=len(payload.terms),
            preview=[t[:40] for t in payload.terms[:3]],
        )
        return payload.terms
    except Exception as exc:
        logger.warning("domhand.oracle_school_llm_terms_failed", error=str(exc)[:120])
        return []


async def oracle_combobox_pick_option_llm(
    canonical_school: str,
    options: list[str],
    search_phrase: str,
) -> int | None:
    """Return 0-based index of the matching option, or None if none fit."""
    school = " ".join(str(canonical_school or "").split()).strip()
    if not school or not options or _oracle_school_llm_disabled():
        return None

    lines = []
    for i, opt in enumerate(options[:_MAX_OPTION_LINES]):
        label = " ".join(str(opt or "").split()).strip()
        if not label:
            continue
        lines.append(f"{i}: {label[:180]}")
    if not lines:
        return None

    body = "\n".join(lines)
    prompt = f"""Oracle HCM school dropdown. Applicant institution (canonical):
\"\"\"{school}\"\"\"

Current search text typed in the box: \"{search_phrase[:120]}\"

Visible options (index: label):
{body}

Task: Pick the ONE option index that refers to the SAME real-world institution as the canonical name (same campus). If none match, or you are unsure, reply with null index.

Reply with ONLY JSON: {{"matched_index": <integer or null>}}
"""
    try:
        text = await _completion_text(prompt, max_tokens=256)
        data = _extract_json_object(text)
        payload = _PickPayload.model_validate(data)
        idx = payload.matched_index
        logger.info(
            "domhand.oracle_school_llm_pick_detail",
            canonical=school[:80],
            search_phrase=search_phrase[:60],
            option_count=len(lines),
            options_sent=body[:500],
            llm_raw=text[:200] if text else "",
            matched_index=idx,
        )
        if idx is None:
            return None
        if idx >= len(options) or idx < 0:
            logger.info("domhand.oracle_school_llm_pick_oob", matched_index=idx, option_count=len(options))
            return None
        return idx
    except Exception as exc:
        logger.warning("domhand.oracle_school_llm_pick_failed", error=str(exc)[:120])
        return None


async def dropdown_pick_option_llm(
    desired_value: str,
    options: list[str],
    *,
    context: str = "skill",
) -> int | None:
    """Generic LLM option picker — GPT-5.4-nano picks the best matching dropdown option.

    Works for skills, schools, fields of study, etc. Returns 0-based index or None.
    """
    desired = " ".join(str(desired_value or "").split()).strip()
    if not desired or not options or _oracle_school_llm_disabled():
        return None

    lines = []
    for i, opt in enumerate(options[:_MAX_OPTION_LINES]):
        label = " ".join(str(opt or "").split()).strip()
        if not label:
            continue
        lines.append(f"{i}: {label[:180]}")
    if not lines:
        return None

    body = "\n".join(lines)
    prompt = f"""Dropdown option matching. The user wants to select:
\"\"\"{desired}\"\"\"

Context: {context}

Visible options (index: label):
{body}

Task: Pick the ONE option index that is the SAME thing as the desired value. For skills, "React" = "React.js" = "ReactJS" (same technology), but "Java" ≠ "JavaScript" and "C" ≠ "C++". If no option matches, reply with null.

Reply with ONLY JSON: {{"matched_index": <integer or null>}}
"""
    try:
        text = await _completion_text(prompt, max_tokens=128)
        data = _extract_json_object(text)
        payload = _PickPayload.model_validate(data)
        idx = payload.matched_index
        logger.info(
            "domhand.dropdown_llm_pick",
            desired=desired[:60],
            context=context,
            option_count=len(lines),
            matched_index=idx,
            llm_raw=text[:200] if text else "",
        )
        if idx is None:
            return None
        if idx >= len(options) or idx < 0:
            return None
        return idx
    except Exception as exc:
        logger.warning("domhand.dropdown_llm_pick_failed", error=str(exc)[:120])
        return None


async def oracle_combobox_verify_commit_llm(
    canonical_school: str,
    committed_ui_text: str,
    picked_option_label: str,
) -> bool:
    """True if the committed UI value refers to the same institution as canonical."""
    school = " ".join(str(canonical_school or "").split()).strip()
    committed = " ".join(str(committed_ui_text or "").split()).strip()
    picked = " ".join(str(picked_option_label or "").split()).strip()
    if not school or not committed:
        return False

    if picked and committed.lower() == picked.lower():
        return True

    if _oracle_school_llm_disabled():
        return False

    prompt = f"""After selecting a school in Oracle HCM, the input shows:
Committed: \"\"\"{committed[:200]}\"\"\"
Picked list row was: \"\"\"{picked[:200]}\"\"\"

Applicant's institution (canonical): \"\"\"{school[:200]}\"\"\"

Does the committed value refer to the SAME real-world institution as the canonical name (same campus)? Reply ONLY JSON: {{"same_institution": true or false}}"""
    try:
        text = await _completion_text(prompt, max_tokens=128)
        data = _extract_json_object(text)
        payload = _VerifyPayload.model_validate(data)
        logger.info(
            "domhand.oracle_school_llm_verify_detail",
            canonical=school[:80],
            committed=committed[:80],
            picked=picked[:80],
            llm_raw=text[:200] if text else "",
            same_institution=payload.same_institution,
        )
        return bool(payload.same_institution)
    except Exception as exc:
        logger.warning("domhand.oracle_school_llm_verify_failed", error=str(exc)[:120])
        return False


class _SchoolLocationPayload(BaseModel):
    city: str = ""
    state: str = ""
    country: str = "United States"


async def oracle_school_location_llm(school_name: str) -> dict[str, str]:
    """Look up school city/state/country using GPT-5.4-nano. Returns dict with city, state, country."""
    name = " ".join(str(school_name or "").split()).strip()
    if not name or _oracle_school_llm_disabled():
        return {}

    prompt = f"""What is the city, state/province, and country of this school/university?

School: \"\"\"{name[:200]}\"\"\"

Reply with ONLY JSON: {{"city": "...", "state": "...", "country": "..."}}
Use the full state name (e.g., "California" not "CA"). Use "United States" for US schools."""

    try:
        text = await _completion_text(prompt, max_tokens=128)
        data = _extract_json_object(text)
        payload = _SchoolLocationPayload.model_validate(data)
        result = {}
        if payload.city.strip():
            result["city"] = payload.city.strip()
        if payload.state.strip():
            result["state"] = payload.state.strip()
        if payload.country.strip():
            result["country"] = payload.country.strip()
        logger.info(
            "domhand.oracle_school_location_llm",
            school=name[:80],
            city=result.get("city", ""),
            state=result.get("state", ""),
            country=result.get("country", ""),
            llm_raw=text[:200] if text else "",
        )
        return result
    except Exception as exc:
        logger.warning("domhand.oracle_school_location_llm_failed", error=str(exc)[:120])
        return {}
