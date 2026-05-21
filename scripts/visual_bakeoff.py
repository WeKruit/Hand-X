"""Run a screenshot-based visual bakeoff on the local toy Workday fixture.

This script is intentionally isolated from Hand-X runtime behavior. It builds a
small deterministic screenshot corpus from the toy Workday app, sends the exact
same screenshots to several candidate multimodal models, and records:

- parsed verification accuracy
- provider/transport reliability
- token usage
- estimated cost
- latency

The goal is to answer a first-principles question before deeper integration:
"Is the visual verification layer strong enough and cheap enough to be worth
building into Hand-X?"
"""

import argparse
import asyncio
import base64
import io
import json
import math
import os
import subprocess
import tempfile
import threading
import time
from enum import StrEnum
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from google import genai
from PIL import Image
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
TOY_WORKDAY_DIR = REPO_ROOT / "examples" / "toy-workday"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "visual_bakeoff"
VIEWPORT = {"width": 1440, "height": 1600}
CONTEXT_PADDING_PX = 84


def _normalize_text(text: str | None) -> str:
    return " ".join(str(text or "").split()).strip().casefold()


def _safe_stem(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value).strip("_") or "item"


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Parse the first JSON object from a model response."""
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json\n", "", 1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no json object found in response: {text[:200]!r}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("response JSON was not an object")
    return data


class CropMode(StrEnum):
    VIEWPORT = "viewport"
    ELEMENT = "element"
    CONTEXT = "context"


class ComparisonMode(StrEnum):
    EXACT = "exact"
    TOKEN_SET = "token_set"


class ModelName(StrEnum):
    GEMINI_FLASH = "gemini-2.5-flash"
    QWEN_36_27B = "Qwen/Qwen3.6-27B"
    QWEN_VL_32B = "Qwen/Qwen3-VL-32B-Instruct"


class ModelConfig(BaseModel):
    name: ModelName
    provider: str
    input_cost_per_million: float
    output_cost_per_million: float
    smoke_enabled: bool = True


class ScenarioSpec(BaseModel):
    scenario_id: str
    description: str
    step: int
    field_label: str
    expected_value: str
    selector: str
    comparison_mode: ComparisonMode = ComparisonMode.EXACT


class ScreenshotArtifact(BaseModel):
    scenario_id: str
    crop_mode: CropMode
    image_path: Path
    selector: str
    width: int
    height: int


class VerificationPayload(BaseModel):
    observed_value: str = ""
    matches_expected: bool | None = None
    confidence: float | None = None


class ModelRunResult(BaseModel):
    scenario_id: str
    crop_mode: CropMode
    model_name: str
    provider: str
    status: str
    expected_value: str
    comparison_mode: ComparisonMode
    observed_value: str | None = None
    matches_expected: bool | None = None
    parsed_match: bool | None = None
    correct: bool | None = None
    confidence: float | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    raw_response: str | None = None
    error: str | None = None


class SmokeGateResult(BaseModel):
    model_name: str
    healthy: bool
    reason: str


MODEL_CONFIGS: dict[ModelName, ModelConfig] = {
    ModelName.GEMINI_FLASH: ModelConfig(
        name=ModelName.GEMINI_FLASH,
        provider="google",
        input_cost_per_million=0.30,
        output_cost_per_million=2.50,
    ),
    ModelName.QWEN_36_27B: ModelConfig(
        name=ModelName.QWEN_36_27B,
        provider="siliconflow",
        input_cost_per_million=0.30,
        output_cost_per_million=3.20,
    ),
    ModelName.QWEN_VL_32B: ModelConfig(
        name=ModelName.QWEN_VL_32B,
        provider="siliconflow",
        input_cost_per_million=0.20,
        output_cost_per_million=0.60,
    ),
}


class ToyServer:
    """Serve the toy Workday fixture from a temporary localhost port."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.root))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = int(self._server.server_address[1])
        return f"http://127.0.0.1:{port}/index.html"

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


async def _page_eval(page: Page, expression: str, arg: Any | None = None) -> Any:
    if arg is None:
        return await page.evaluate(expression)
    return await page.evaluate(expression, arg)


async def show_step(page: Page, step: int) -> None:
    await _page_eval(page, "(step) => { showStep(step); }", step)


async def set_text_value(page: Page, selector: str, value: str) -> None:
    await _page_eval(
        page,
        """({ selector, value }) => {
            const el = document.querySelector(selector);
            if (!el) throw new Error(`Missing text input: ${selector}`);
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        {"selector": selector, "value": value},
    )


async def set_checkbox_value(page: Page, selector: str, checked: bool) -> None:
    await _page_eval(
        page,
        """({ selector, checked }) => {
            const el = document.querySelector(selector);
            if (!el) throw new Error(`Missing checkbox: ${selector}`);
            el.checked = checked;
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        {"selector": selector, "checked": checked},
    )


async def set_radio_value(page: Page, name: str, value: str) -> None:
    await _page_eval(
        page,
        """({ name, value }) => {
            const selector = `input[name="${name}"][value="${value}"]`;
            const el = document.querySelector(selector);
            if (!el) throw new Error(`Missing radio input: ${selector}`);
            el.checked = true;
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        {"name": name, "value": value},
    )


async def commit_dropdown_value(page: Page, button_selector: str, value: str) -> None:
    await _page_eval(
        page,
        """({ buttonSelector, value }) => {
            const button = document.querySelector(buttonSelector);
            if (!button) throw new Error(`Missing dropdown button: ${buttonSelector}`);
            const dropdown = button.closest('.wd-dropdown');
            if (!dropdown) throw new Error(`Missing dropdown wrapper: ${buttonSelector}`);
            const key = dropdown.getAttribute('data-dropdown');
            const popup = document.querySelector(`.wd-popup[data-for="${key}"]`);
            if (!popup) throw new Error(`Missing dropdown popup for ${key}`);
            commitDropdown(button, popup, value);
        }""",
        {"buttonSelector": button_selector, "value": value},
    )


async def commit_prompt_value(page: Page, widget_selector: str, value: str, *, clear_first: bool = True) -> None:
    await _page_eval(
        page,
        """({ widgetSelector, value, clearFirst }) => {
            const widget = document.querySelector(widgetSelector);
            if (!widget) throw new Error(`Missing prompt widget: ${widgetSelector}`);
            if (clearFirst) clearPromptSelection(widget);
            commitPromptSelection(widget, value);
        }""",
        {"widgetSelector": widget_selector, "value": value, "clearFirst": clear_first},
    )


async def commit_prompt_from_input(page: Page, input_selector: str, value: str, *, clear_first: bool = True) -> None:
    await _page_eval(
        page,
        """({ inputSelector, value, clearFirst }) => {
            const input = document.querySelector(inputSelector);
            if (!input) throw new Error(`Missing prompt input: ${inputSelector}`);
            const widget = input.closest('.wd-prompt');
            if (!widget) throw new Error(`Missing prompt widget for: ${inputSelector}`);
            if (clearFirst) clearPromptSelection(widget);
            commitPromptSelection(widget, value);
        }""",
        {"inputSelector": input_selector, "value": value, "clearFirst": clear_first},
    )


async def add_repeat_entry(page: Page, kind: str) -> None:
    await _page_eval(page, "(kind) => { insertRepeatEntry(kind); }", kind)


async def set_date_value(page: Page, wrapper_selector: str, value: str) -> None:
    await _page_eval(
        page,
        """({ wrapperSelector, value }) => {
            const wrapper = document.querySelector(wrapperSelector);
            if (!wrapper) throw new Error(`Missing date wrapper: ${wrapperSelector}`);
            setDateValue(wrapper, value);
        }""",
        {"wrapperSelector": wrapper_selector, "value": value},
    )


async def render_review(page: Page) -> None:
    await _page_eval(page, "() => { renderReview(); }")


async def settle(page: Page) -> None:
    await page.wait_for_timeout(150)


async def seed_full_application(page: Page) -> None:
    """Create a representative fully filled application state."""
    await show_step(page, 0)
    await commit_prompt_value(page, '#section-0 .wd-prompt[data-tree-key="referral-source"]', "LinkedIn")
    await set_radio_value(page, "candidateIsPreviousWorker", "Yes")
    await set_text_value(page, "#previousWorker--email", "former.worker@workday.com")
    await set_text_value(page, "#previousWorker--manager", "Taylor Manager")
    await set_text_value(page, "#previousWorker--location", "Pleasanton")
    await set_text_value(page, "#previousWorker--employeeID", "WD-104403")
    await commit_dropdown_value(page, "#address--countryRegion", "Virginia")
    await set_text_value(page, "#name--legalName--firstName", "Spencer")
    await set_text_value(page, "#name--legalName--middleName", "Young")
    await set_text_value(page, "#name--legalName--lastName", "Wang")
    await set_checkbox_value(page, "#name--preferredCheck", True)
    await set_text_value(page, "#name--preferredName", "Spence")
    await set_text_value(page, "#address--addressLine1", "123 Main Street")
    await set_text_value(page, "#address--city", "Chantilly")
    await set_text_value(page, "#address--postalCode", "20151")
    await set_text_value(page, "#contactInformation--email", "spencer@example.com")
    await commit_dropdown_value(page, "#phone--deviceType", "Mobile")
    await commit_prompt_from_input(page, "#phone--countryPhoneCode", "United States of America (+1)")
    await set_text_value(page, "#phone--phoneNumber", "5717788080")
    await set_text_value(page, "#phone--phoneExtension", "204")
    await settle(page)

    await show_step(page, 1)
    await add_repeat_entry(page, "work-experience")
    await set_text_value(page, "#workExperience-1--jobTitle", "Software Engineer")
    await set_text_value(page, "#workExperience-1--companyName", "Acme Systems")
    await set_text_value(page, "#workExperience-1--location", "Chantilly")
    await set_date_value(
        page,
        '#work-experience-entries .wd-repeat-entry[data-entry-index="1"] [data-automation-id="formField-startDate"] .wd-date-wrapper',
        "01/2026",
    )
    await set_date_value(
        page,
        '#work-experience-entries .wd-repeat-entry[data-entry-index="1"] [data-automation-id="formField-endDate"] .wd-date-wrapper',
        "04/2026",
    )
    await set_text_value(page, "#workExperience-1--roleDescription", "Built internal workflow tooling.")
    await add_repeat_entry(page, "education")
    await commit_prompt_from_input(page, "#education-1--school", "University of Virginia")
    await commit_dropdown_value(page, "#education-1--degree", "Bachelor of Science")
    await commit_prompt_from_input(page, "#education-1--fieldOfStudy", "Computer Science")
    await set_text_value(page, "#education-1--overallResult", "3.9")
    await commit_prompt_value(page, '[data-multiselect="skills"]', "Python", clear_first=True)
    await commit_prompt_value(page, '[data-multiselect="skills"]', "TypeScript", clear_first=False)
    await commit_prompt_value(page, '[data-multiselect="skills"]', "Playwright", clear_first=False)
    await set_text_value(page, "#socialNetworkAccounts--linkedInAccount", "linkedin.com/in/spencer")
    await set_text_value(page, "#socialNetworkAccounts--twitterAccount", "@spencer")
    await settle(page)

    await show_step(page, 2)
    await commit_dropdown_value(page, "#questions--relocate", "Yes, I would consider relocating for this role")
    await commit_dropdown_value(page, "#questions--nonCompete", "No")
    await commit_dropdown_value(page, "#questions--workdaySystem", "Yes")
    await commit_dropdown_value(page, "#questions--authorized", "Yes")
    await commit_dropdown_value(page, "#questions--visa", "No")
    await commit_dropdown_value(page, "#questions--government", "Yes")
    await commit_dropdown_value(page, "#questions--contracting", "No")
    await commit_dropdown_value(page, "#questions--sanctions", "No")
    await commit_dropdown_value(page, "#questions--relatedEmployee", "Yes")
    await set_text_value(page, "#questions--relationshipDetails", "Jordan Wang, sibling, Solutions Consultant.")
    await commit_dropdown_value(page, "#questions--customerRelationship", "No")
    await commit_dropdown_value(page, "#questions--acknowledge", "Yes")
    await settle(page)

    await show_step(page, 3)
    await commit_dropdown_value(page, "#agreements--nda", "I have read and agree to the Non Disclosure Agreement")
    await commit_dropdown_value(
        page, "#agreements--arbitration", "I have read and agree to the Mutual Arbitration Agreement"
    )
    await set_text_value(page, "#agreements--name", "Spencer Wang")
    await set_date_value(page, '#section-3 .wd-formfield[data-field-kind="date-mdy"] .wd-date-wrapper', "04/20/2026")
    await settle(page)

    await show_step(page, 4)
    await commit_dropdown_value(page, "#disclosures--gender", "Male")
    await commit_dropdown_value(page, "#disclosures--ethnicity", "Asian (Not Hispanic or Latino)")
    await commit_dropdown_value(page, "#disclosures--veteran", "I am not a protected veteran")
    await set_checkbox_value(page, "#disclosures--terms", True)
    await settle(page)

    await show_step(page, 5)
    await commit_dropdown_value(page, "#selfIdentify--language", "English")
    await set_text_value(page, "#selfIdentify--name", "Spencer Wang")
    await set_text_value(page, "#selfIdentify--employeeId", "N/A")
    await set_date_value(page, '#section-5 .wd-formfield[data-field-kind="date-mdy"] .wd-date-wrapper', "04/21/2026")
    await set_radio_value(page, "selfIdentifyDisability", "I do not want to answer")
    await settle(page)

    await show_step(page, 6)
    await render_review(page)
    await settle(page)


SCENARIOS: list[ScenarioSpec] = [
    ScenarioSpec(
        scenario_id="source_prompt_selected",
        description="Referral source tree prompt with a selected item chip.",
        step=0,
        field_label="How Did You Hear About Us?",
        expected_value="LinkedIn",
        selector="#section-0 .wd-formfield:has(#source--source)",
    ),
    ScenarioSpec(
        scenario_id="phone_code_selected",
        description="Phone country code prompt-search widget with a single selected chip.",
        step=0,
        field_label="Country / Territory Phone Code",
        expected_value="United States of America (+1)",
        selector="#section-0 .wd-formfield:has(#phone--countryPhoneCode)",
    ),
    ScenarioSpec(
        scenario_id="previous_worker_yes",
        description="Yes/No radio group with the Yes option selected.",
        step=0,
        field_label="Have you previously worked for or are you currently working for Workday as an employee or contractor?",
        expected_value="Yes",
        selector="#section-0 .wd-formfield:has(#previousWorker--candidateIsPreviousWorker)",
    ),
    ScenarioSpec(
        scenario_id="state_dropdown_selected",
        description="Standard dropdown showing a selected state value.",
        step=0,
        field_label="State",
        expected_value="Virginia",
        selector="#section-0 .wd-formfield:has(#address--countryRegion)",
    ),
    ScenarioSpec(
        scenario_id="preferred_name_filled",
        description="Conditionally revealed preferred name field with text entered.",
        step=0,
        field_label="Preferred Name",
        expected_value="Spence",
        selector="#preferred-name-row .wd-formfield:has(#name--preferredName)",
    ),
    ScenarioSpec(
        scenario_id="skills_multiselect",
        description="Skills multiselect showing multiple selected chips.",
        step=1,
        field_label="Type to Add Skills",
        expected_value="Python, TypeScript, Playwright",
        selector='[data-automation-id="formField-skills"]',
        comparison_mode=ComparisonMode.TOKEN_SET,
    ),
    ScenarioSpec(
        scenario_id="education_school_prompt",
        description="Education repeater row with a selected school prompt value.",
        step=1,
        field_label="School or University",
        expected_value="University of Virginia",
        selector='#education-entries .wd-repeat-entry[data-entry-index="1"] [data-automation-id="formField-school"]',
    ),
    ScenarioSpec(
        scenario_id="questions_government_yes",
        description="Application question dropdown with conditional reveal trigger set to Yes.",
        step=2,
        field_label="Are you a current or former employee of the United States government?",
        expected_value="Yes",
        selector="#section-2 .wd-formfield:has(#questions--government)",
    ),
    ScenarioSpec(
        scenario_id="agreements_date_selected",
        description="Segmented MM/DD/YYYY date field on Application Questions 2 of 2.",
        step=3,
        field_label="Please enter today's date:",
        expected_value="04/20/2026",
        selector='#section-3 .wd-formfield[data-field-kind="date-mdy"]',
    ),
    ScenarioSpec(
        scenario_id="self_identify_radio_selected",
        description="Boolean-card radio group on the self-identify page.",
        step=5,
        field_label="Please check one of the boxes below:",
        expected_value="I do not want to answer",
        selector="#section-5 .wd-formfield:has(#selfIdentify--disability)",
    ),
    ScenarioSpec(
        scenario_id="review_source_row",
        description="Review page readback row for the referral source.",
        step=6,
        field_label="Review - How Did You Hear About Us?",
        expected_value="LinkedIn",
        selector="#section-6 .wd-review-card:nth-of-type(1) .wd-review-row:nth-of-type(1)",
    ),
]


def _compare_observed_value(observed_value: str | None, expected_value: str, mode: ComparisonMode) -> bool:
    observed = _normalize_text(observed_value)
    expected = _normalize_text(expected_value)
    if mode is ComparisonMode.EXACT:
        return observed == expected
    expected_tokens = [token.strip() for token in expected.split(",") if token.strip()]
    return bool(expected_tokens) and all(token in observed for token in expected_tokens)


def _estimate_cost_usd(input_tokens: int | None, output_tokens: int | None, config: ModelConfig) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * config.input_cost_per_million + output_tokens * config.output_cost_per_million) / 1_000_000


def _context_clip(box: Any, viewport: dict[str, int]) -> dict[str, float]:
    x = max(box["x"] - CONTEXT_PADDING_PX, 0)
    y = max(box["y"] - CONTEXT_PADDING_PX, 0)
    max_width = viewport["width"] - x
    max_height = viewport["height"] - y
    width = min(box["width"] + 2 * CONTEXT_PADDING_PX, max_width)
    height = min(box["height"] + 2 * CONTEXT_PADDING_PX, max_height)
    return {
        "x": math.floor(x),
        "y": math.floor(y),
        "width": math.ceil(width),
        "height": math.ceil(height),
    }


async def _new_page(browser: Browser) -> Page:
    context = await browser.new_context(viewport=cast(Any, VIEWPORT), device_scale_factor=2)
    page = await context.new_page()
    return page


async def capture_artifact(
    page: Page, scenario: ScenarioSpec, crop_mode: CropMode, output_dir: Path
) -> ScreenshotArtifact:
    locator = page.locator(scenario.selector).first
    await locator.scroll_into_view_if_needed()
    await page.wait_for_timeout(100)
    image_dir = output_dir / "screenshots" / scenario.scenario_id
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / f"{scenario.scenario_id}__{crop_mode.value}.png"

    if crop_mode is CropMode.VIEWPORT:
        await page.screenshot(path=str(image_path), full_page=False)
        width, height = VIEWPORT["width"], VIEWPORT["height"]
    elif crop_mode is CropMode.ELEMENT:
        await locator.screenshot(path=str(image_path), type="png")
        box = await locator.bounding_box()
        if box is None:
            raise RuntimeError(f"Could not compute bounding box for {scenario.selector}")
        width, height = math.ceil(box["width"]), math.ceil(box["height"])
    else:
        box = await locator.bounding_box()
        if box is None:
            raise RuntimeError(f"Could not compute bounding box for {scenario.selector}")
        clip = _context_clip(box, VIEWPORT)
        await page.screenshot(path=str(image_path), clip=cast(Any, clip), type="png")
        width, height = int(clip["width"]), int(clip["height"])

    return ScreenshotArtifact(
        scenario_id=scenario.scenario_id,
        crop_mode=crop_mode,
        image_path=image_path,
        selector=scenario.selector,
        width=width,
        height=height,
    )


def _build_prompt(scenario: ScenarioSpec) -> str:
    return (
        "You are verifying a single Workday-style form control screenshot. "
        "Return JSON only with keys observed_value (string), matches_expected (boolean), confidence (number). "
        f'Field label: "{scenario.field_label}". '
        f'Expected value: "{scenario.expected_value}". '
        "Use observed_value for the visibly selected, filled, or read-back value for this field."
    )


def _siliconflow_payload(model_name: ModelName, screenshot_path: Path, prompt: str) -> dict[str, Any]:
    with Image.open(screenshot_path) as img:
        converted = img.convert("RGB")
        raw = io.BytesIO()
        converted.save(raw, format="WEBP", quality=90)
    payload: dict[str, Any] = {
        "model": model_name.value,
        "stream": False,
        "max_tokens": 220,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/webp;base64,{base64.b64encode(raw.getvalue()).decode('ascii')}",
                            "detail": "low",
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    if model_name is ModelName.QWEN_36_27B:
        payload["enable_thinking"] = False
    return payload


def _run_siliconflow_curl(payload: dict[str, Any], api_key: str, *, max_time_seconds: int) -> tuple[str, int]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
        payload_path = handle.name
    try:
        result = subprocess.run(
            [
                "curl",
                "--max-time",
                str(max_time_seconds),
                "-sS",
                "https://api.siliconflow.com/v1/chat/completions",
                "-H",
                f"Authorization: Bearer {api_key}",
                "-H",
                "Content-Type: application/json",
                "--data",
                f"@{payload_path}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout or result.stderr, int(result.returncode)
    finally:
        os.unlink(payload_path)


def run_model_on_artifact(
    config: ModelConfig,
    screenshot: ScreenshotArtifact,
    scenario: ScenarioSpec,
    *,
    google_client: genai.Client | None,
    google_api_key: str | None,
    siliconflow_api_key: str | None,
    siliconflow_curl_max_time: int,
) -> ModelRunResult:
    prompt = _build_prompt(scenario)
    started = time.perf_counter()

    try:
        if config.provider == "google":
            if google_client is None or not google_api_key:
                raise RuntimeError("GOOGLE_API_KEY is missing")
            image = Image.open(screenshot.image_path).convert("RGB")
            response = google_client.models.generate_content(
                model=config.name.value,
                contents=[prompt, image],
                config={
                    "temperature": 0,
                    "response_mime_type": "application/json",
                    "thinking_config": {"thinking_budget": 0},
                    "max_output_tokens": 220,
                },
            )
            raw_response = response.text or ""
            usage = getattr(response, "usage_metadata", None)
            input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        else:
            if not siliconflow_api_key:
                raise RuntimeError("SILLICON_FLOW_KEY is missing")
            payload = _siliconflow_payload(config.name, screenshot.image_path, prompt)
            raw_transport, returncode = _run_siliconflow_curl(
                payload,
                siliconflow_api_key,
                max_time_seconds=siliconflow_curl_max_time,
            )
            if returncode != 0:
                raise RuntimeError(f"curl returned {returncode}: {raw_transport[:400]}")
            transport_data = json.loads(raw_transport)
            if "choices" not in transport_data:
                code = transport_data.get("code")
                message = transport_data.get("message", "unknown SiliconFlow error")
                raise RuntimeError(f"siliconflow_error code={code}: {message}")
            message = (transport_data["choices"][0] or {}).get("message") or {}
            raw_response = str(message.get("content") or "")
            usage = transport_data.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0) or 0)
            output_tokens = int(usage.get("completion_tokens", 0) or 0)

        parsed = VerificationPayload.model_validate(_extract_json_object(raw_response))
        correct = _compare_observed_value(parsed.observed_value, scenario.expected_value, scenario.comparison_mode)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ModelRunResult(
            scenario_id=scenario.scenario_id,
            crop_mode=screenshot.crop_mode,
            model_name=config.name.value,
            provider=config.provider,
            status="success",
            expected_value=scenario.expected_value,
            comparison_mode=scenario.comparison_mode,
            observed_value=parsed.observed_value,
            matches_expected=parsed.matches_expected,
            parsed_match=correct,
            correct=correct,
            confidence=parsed.confidence,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=_estimate_cost_usd(input_tokens, output_tokens, config),
            raw_response=raw_response,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ModelRunResult(
            scenario_id=scenario.scenario_id,
            crop_mode=screenshot.crop_mode,
            model_name=config.name.value,
            provider=config.provider,
            status="error",
            expected_value=scenario.expected_value,
            comparison_mode=scenario.comparison_mode,
            latency_ms=latency_ms,
            error=str(exc),
        )


def summarize_results(results: list[ModelRunResult]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    by_model: dict[str, list[ModelRunResult]] = {}
    for result in results:
        by_model.setdefault(result.model_name, []).append(result)

    for model_name, model_results in by_model.items():
        successes = [item for item in model_results if item.status == "success"]
        completed = [item for item in successes if item.correct is not None]
        accuracy = (sum(1 for item in completed if item.correct) / len(completed)) if completed else None
        avg_latency = (
            (sum(item.latency_ms or 0 for item in model_results) / len(model_results)) if model_results else None
        )
        total_cost = sum(item.estimated_cost_usd or 0.0 for item in successes)
        avg_cost = (total_cost / len(successes)) if successes else None
        summary[model_name] = {
            "calls": len(model_results),
            "successful_calls": len(successes),
            "error_calls": len(model_results) - len(successes),
            "accuracy": accuracy,
            "avg_latency_ms": avg_latency,
            "avg_cost_usd": avg_cost,
            "total_cost_usd": total_cost,
        }
    return summary


def render_markdown_summary(summary: dict[str, Any], smoke: list[SmokeGateResult], output_dir: Path) -> str:
    lines = [
        "# Visual Bakeoff Summary",
        "",
        f"- Output dir: `{output_dir}`",
        "",
        "## Smoke Gate",
        "",
    ]
    for gate in smoke:
        status = "healthy" if gate.healthy else "unhealthy"
        lines.append(f"- `{gate.model_name}`: **{status}** — {gate.reason}")
    lines.extend(["", "## Model Metrics", ""])
    for model_name, stats in summary.items():
        lines.append(f"### `{model_name}`")
        lines.append(f"- Calls: {stats['calls']}")
        lines.append(f"- Successful calls: {stats['successful_calls']}")
        lines.append(f"- Error calls: {stats['error_calls']}")
        lines.append(f"- Accuracy: {stats['accuracy']}")
        lines.append(f"- Avg latency (ms): {stats['avg_latency_ms']}")
        lines.append(f"- Avg cost (USD): {stats['avg_cost_usd']}")
        lines.append(f"- Total cost (USD): {stats['total_cost_usd']}")
        lines.append("")
    return "\n".join(lines)


def write_outputs(
    output_dir: Path,
    results: list[ModelRunResult],
    smoke_results: list[SmokeGateResult],
    *,
    final_summary: dict[str, Any] | None = None,
) -> None:
    (output_dir / "results.json").write_text(
        json.dumps([result.model_dump(mode="json") for result in results], indent=2)
    )
    (output_dir / "smoke_gate.json").write_text(
        json.dumps([gate.model_dump(mode="json") for gate in smoke_results], indent=2)
    )
    if final_summary is not None:
        (output_dir / "summary.json").write_text(json.dumps(final_summary, indent=2))
        markdown = render_markdown_summary(final_summary, smoke_results, output_dir)
        (output_dir / "SUMMARY.md").write_text(markdown)


async def build_screenshot_corpus(
    browser: Browser,
    toy_url: str,
    scenarios: list[ScenarioSpec],
    crop_modes: list[CropMode],
    output_dir: Path,
) -> dict[tuple[str, CropMode], ScreenshotArtifact]:
    artifacts: dict[tuple[str, CropMode], ScreenshotArtifact] = {}
    for scenario in scenarios:
        context: BrowserContext = await browser.new_context(viewport=cast(Any, VIEWPORT), device_scale_factor=2)
        page = await context.new_page()
        await page.goto(toy_url)
        await page.wait_for_load_state("networkidle")
        await seed_full_application(page)
        await show_step(page, scenario.step)
        await settle(page)
        for crop_mode in crop_modes:
            artifact = await capture_artifact(page, scenario, crop_mode, output_dir)
            artifacts[(scenario.scenario_id, crop_mode)] = artifact
        await context.close()
    return artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the toy Workday visual model bakeoff.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / time.strftime("%Y%m%d-%H%M%S"),
        help="Where screenshots and result files should be written.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=[member.value for member in ModelName],
        default=[member.value for member in ModelName],
        help="Subset of models to evaluate.",
    )
    parser.add_argument(
        "--crop-modes",
        nargs="+",
        choices=[member.value for member in CropMode],
        default=[member.value for member in CropMode],
        help="Subset of crop modes to evaluate.",
    )
    parser.add_argument(
        "--scenario-limit",
        type=int,
        default=None,
        help="Limit the number of scenarios for a faster first pass.",
    )
    parser.add_argument(
        "--skip-smoke-gate",
        action="store_true",
        help="Run all selected models even if a model fails the smoke gate.",
    )
    parser.add_argument(
        "--siliconflow-curl-max-time",
        type=int,
        default=25,
        help="Hard wall-clock timeout in seconds for SiliconFlow curl calls.",
    )
    return parser.parse_args()


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    google_api_key = os.getenv("GOOGLE_API_KEY")
    siliconflow_api_key = os.getenv("SILLICON_FLOW_KEY")

    args = parse_args()
    output_dir: Path = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_models = [MODEL_CONFIGS[ModelName(model)] for model in args.models]
    selected_crop_modes = [CropMode(mode) for mode in args.crop_modes]
    selected_scenarios = SCENARIOS[: args.scenario_limit] if args.scenario_limit else list(SCENARIOS)
    scenario_by_id = {scenario.scenario_id: scenario for scenario in selected_scenarios}
    google_client = genai.Client(api_key=google_api_key) if google_api_key else None

    with ToyServer(TOY_WORKDAY_DIR) as toy_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            artifacts = await build_screenshot_corpus(
                browser, toy_url, selected_scenarios, selected_crop_modes, output_dir
            )
            await browser.close()

    smoke_crop_mode = CropMode.CONTEXT if CropMode.CONTEXT in selected_crop_modes else selected_crop_modes[0]
    smoke_artifact = (
        artifacts[(selected_scenarios[1].scenario_id, smoke_crop_mode)]
        if len(selected_scenarios) > 1
        else next(iter(artifacts.values()))
    )
    smoke_scenario = scenario_by_id[smoke_artifact.scenario_id]
    smoke_results: list[SmokeGateResult] = []
    healthy_models: list[ModelConfig] = []

    for config in selected_models:
        smoke_run = run_model_on_artifact(
            config,
            smoke_artifact,
            smoke_scenario,
            google_client=google_client,
            google_api_key=google_api_key,
            siliconflow_api_key=siliconflow_api_key,
            siliconflow_curl_max_time=args.siliconflow_curl_max_time,
        )
        healthy = smoke_run.status == "success"
        reason = "parseable visual response" if healthy else (smoke_run.error or "unknown smoke failure")
        smoke_results.append(SmokeGateResult(model_name=config.name.value, healthy=healthy, reason=reason))
        if healthy or args.skip_smoke_gate:
            healthy_models.append(config)
    write_outputs(output_dir, [], smoke_results)

    results: list[ModelRunResult] = []
    total_calls = len(healthy_models) * len(selected_scenarios) * len(selected_crop_modes)
    for config in healthy_models:
        for scenario in selected_scenarios:
            for crop_mode in selected_crop_modes:
                artifact = artifacts[(scenario.scenario_id, crop_mode)]
                result = run_model_on_artifact(
                    config,
                    artifact,
                    scenario,
                    google_client=google_client,
                    google_api_key=google_api_key,
                    siliconflow_api_key=siliconflow_api_key,
                    siliconflow_curl_max_time=args.siliconflow_curl_max_time,
                )
                results.append(result)
                print(
                    f"[{len(results)}/{total_calls}] {result.model_name} "
                    f"{result.scenario_id} {result.crop_mode}: {result.status}",
                    flush=True,
                )
                write_outputs(output_dir, results, smoke_results)

    summary = summarize_results(results)
    write_outputs(output_dir, results, smoke_results, final_summary=summary)
    markdown = render_markdown_summary(summary, smoke_results, output_dir)
    print(markdown)


if __name__ == "__main__":
    asyncio.run(main())
