"""Measure batched real-Workday screenshot verification cost with Gemini.

This script is intentionally isolated from Hand-X runtime behavior. It uses the
real Workday screenshots captured by the user, pushes them through the same
Browser Use message/screenshot path we are evaluating for the visual layer, and
records the cost/accuracy of verifying multiple visible fields in one Gemini
call.

The primary question this script answers is:

"If we batch several explicit visible fields from one real Workday screenshot
into a single Gemini 3 Flash Preview verification call, what does that call
cost and what rough whole-flow price does that imply?"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from visual_bakeoff import REPO_ROOT

from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import SerializedDOMState
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.messages import SystemMessage
from browser_use.tokens.service import TokenCost

OFFICIAL_GEMINI_3_FLASH_PREVIEW_INPUT_COST_PER_MILLION = 0.50
OFFICIAL_GEMINI_3_FLASH_PREVIEW_OUTPUT_COST_PER_MILLION = 3.00
DEFAULT_SCREENSHOT_DIR = Path("/Users/spencerwang/Desktop/WorkdayTestScreenshots")
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "real_workday_batch_verification"
DEFAULT_MODEL_NAME = "gemini-3-flash-preview"


class ExpectedField(BaseModel):
    field_label: str
    expected_value: str
    field_type_hint: str = "text"


class ScreenshotSpec(BaseModel):
    page_id: str
    image_name: str
    page_label: str
    source_url: str
    fields: list[ExpectedField]


class BatchFieldVerification(BaseModel):
    field_label: str
    observed_value: str = ""
    matches_expected: bool | None = None
    confidence: float | None = None


class BatchVerificationPayload(BaseModel):
    page_id: str
    fields: list[BatchFieldVerification]


class FieldResult(BaseModel):
    field_label: str
    expected_value: str
    observed_value: str | None = None
    matches_expected: bool | None = None
    correct: bool | None = None
    confidence: float | None = None


class BatchExperimentRow(BaseModel):
    page_id: str
    page_label: str
    image_name: str
    image_path: str
    field_count: int
    reasoning_mode: str
    resize_mode: str
    browser_use_mode: str
    model_name: str
    status: str
    fields_correct: int | None = None
    all_fields_correct: bool | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    prompt_image_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    browser_use_reported_cost_usd: float | None = None
    official_price_cost_usd: float | None = None
    effective_cost_per_field_usd: float | None = None
    screenshot_dimensions: str | None = None
    field_results: list[FieldResult] = Field(default_factory=list)
    raw_response: str | None = None
    error: str | None = None


WORKDAY_SCREENSHOT_SPECS: list[ScreenshotSpec] = [
    ScreenshotSpec(
        page_id="workday_real_001_my_information_top",
        image_name="Screenshot 2026-05-21 at 5.59.38\u202fPM.png",
        page_label="My Information (top section)",
        source_url="https://workday.wd5.myworkdayjobs.com/",
        fields=[
            ExpectedField(
                field_label="Have you previously worked for or are you currently working for Workday as an employee or contractor?",
                expected_value="No",
                field_type_hint="radio_group",
            ),
            ExpectedField(
                field_label="Country / Territory", expected_value="United States of America", field_type_hint="dropdown"
            ),
            ExpectedField(field_label="First Name", expected_value="Spencer"),
            ExpectedField(field_label="Middle Name", expected_value=""),
            ExpectedField(field_label="Last Name", expected_value="Wang"),
        ],
    ),
    ScreenshotSpec(
        page_id="workday_real_002_my_information_contact",
        image_name="Screenshot 2026-05-21 at 6.10.27\u202fPM.png",
        page_label="My Information (contact section)",
        source_url="https://workday.wd5.myworkdayjobs.com/",
        fields=[
            ExpectedField(field_label="Postal Code", expected_value=""),
            ExpectedField(field_label="Email Address", expected_value="spencerycwang@ucla.edu"),
            ExpectedField(field_label="Phone Device Type", expected_value="Mobile", field_type_hint="dropdown"),
            ExpectedField(
                field_label="Country / Territory Phone Code",
                expected_value="United States of America (+1)",
                field_type_hint="prompt_chip",
            ),
            ExpectedField(field_label="Phone Number", expected_value="571-778-8080"),
            ExpectedField(field_label="Phone Extension", expected_value=""),
        ],
    ),
    ScreenshotSpec(
        page_id="workday_real_003_experience_repeater",
        image_name="Screenshot 2026-05-21 at 6.14.08\u202fPM.png",
        page_label="My Experience (repeater row)",
        source_url="https://workday.wd5.myworkdayjobs.com/",
        fields=[
            ExpectedField(field_label="Job Title", expected_value="Software Developer"),
            ExpectedField(field_label="Company", expected_value="Bruin Formula Racing"),
            ExpectedField(field_label="Location", expected_value="Los Angeles CA"),
            ExpectedField(field_label="I currently work here", expected_value="checked", field_type_hint="checkbox"),
            ExpectedField(field_label="From", expected_value="09/2025", field_type_hint="date"),
        ],
    ),
    ScreenshotSpec(
        page_id="workday_real_004_application_questions_1",
        image_name="Screenshot 2026-05-21 at 6.25.56\u202fPM.png",
        page_label="Application Questions 1 of 2",
        source_url="https://workday.wd5.myworkdayjobs.com/",
        fields=[
            ExpectedField(
                field_label="Would you consider relocating for this role?",
                expected_value="I am local to where the job is posted",
                field_type_hint="dropdown",
            ),
            ExpectedField(
                field_label="Are you subject to any non-compete or non-solicitation restrictions at your current or most recent employer?",
                expected_value="No",
                field_type_hint="dropdown",
            ),
            ExpectedField(
                field_label="In your current job, do you use or work on the Workday system?",
                expected_value="Select One",
                field_type_hint="dropdown",
            ),
            ExpectedField(
                field_label="Are you authorized to work in the country where this job is located?",
                expected_value="Select One",
                field_type_hint="dropdown",
            ),
        ],
    ),
]


@dataclass(frozen=True)
class _BatchPromptContext:
    screenshot_b64: str
    screenshot_dimensions: str
    browser_state: BrowserStateSummary


def _normalize_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def _normalize_observed_value(value: str | None) -> str:
    normalized = _normalize_text(value)
    if normalized in {"", "blank", "empty", "not filled", "unfilled", "none", "n/a"}:
        return ""
    return normalized


def _build_system_message() -> SystemMessage:
    return SystemMessage(
        content=(
            "You are a careful visual verifier for real Workday application screenshots. "
            "Verify only the explicitly listed fields. "
            "Return JSON with `page_id` and a `fields` array. "
            "Each field entry must include `field_label`, `observed_value`, `matches_expected`, and `confidence`. "
            "Use `observed_value` of an empty string when a text field is visibly blank. "
            "For dropdowns, return the visible selected text. "
            "For radio groups, return the selected option text. "
            "For standalone checkboxes, return `checked` or `unchecked`. "
            "Do not invent fields that are not clearly visible."
        )
    )


def _build_task(spec: ScreenshotSpec) -> str:
    field_lines = []
    for field in spec.fields:
        expected = field.expected_value if field.expected_value else "[empty string]"
        field_lines.append(
            f'- label="{field.field_label}" | type="{field.field_type_hint}" | expected_visible_value="{expected}"'
        )
    joined = "\n".join(field_lines)
    return (
        "This is a visual verification benchmark on a real Workday application screenshot.\n"
        f'Page id: "{spec.page_id}"\n'
        f'Page label: "{spec.page_label}"\n'
        "Verify only the following visible fields and compare each one against the expected visible value.\n"
        f"{joined}\n"
        "Return structured output only."
    )


def _official_cost_usd(prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (
        prompt_tokens * OFFICIAL_GEMINI_3_FLASH_PREVIEW_INPUT_COST_PER_MILLION
        + completion_tokens * OFFICIAL_GEMINI_3_FLASH_PREVIEW_OUTPUT_COST_PER_MILLION
    ) / 1_000_000


def _screenshot_dimensions_from_bytes(image_bytes: bytes) -> str:
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as image:
        return f"{image.size[0]}x{image.size[1]}"


def _build_browser_state(spec: ScreenshotSpec) -> BrowserStateSummary:
    return BrowserStateSummary(
        dom_state=SerializedDOMState(_root=None, selector_map={}),
        url=spec.source_url,
        title=spec.page_label,
        tabs=[TabInfo(url=spec.source_url, title=spec.page_label, target_id="tab-real-workday-0001")],
        screenshot=None,
    )


def _load_screenshot_context(spec: ScreenshotSpec, screenshot_dir: Path) -> _BatchPromptContext:
    image_path = screenshot_dir / spec.image_name
    image_bytes = image_path.read_bytes()
    return _BatchPromptContext(
        screenshot_b64=base64.b64encode(image_bytes).decode("ascii"),
        screenshot_dimensions=_screenshot_dimensions_from_bytes(image_bytes),
        browser_state=_build_browser_state(spec),
    )


async def _build_prompt_message(
    spec: ScreenshotSpec,
    screenshot_dir: Path,
    file_system: FileSystem,
) -> tuple[Any, _BatchPromptContext]:
    context = _load_screenshot_context(spec, screenshot_dir)
    prompt = AgentMessagePrompt(
        browser_state_summary=context.browser_state,
        file_system=file_system,
        task=_build_task(spec),
        screenshots=[context.screenshot_b64],
        vision_detail_level="auto",
        llm_screenshot_size=None,
    )
    return prompt.get_user_message(use_vision=True), context


def _evaluate_field(expected: ExpectedField, observed: BatchFieldVerification | None) -> FieldResult:
    observed_value = observed.observed_value if observed else None
    correct = None
    if observed is not None:
        correct = _normalize_observed_value(observed.observed_value) == _normalize_observed_value(
            expected.expected_value
        )
    return FieldResult(
        field_label=expected.field_label,
        expected_value=expected.expected_value,
        observed_value=observed_value,
        matches_expected=observed.matches_expected if observed else None,
        correct=correct,
        confidence=observed.confidence if observed else None,
    )


async def _invoke_batch_verifier(
    spec: ScreenshotSpec,
    screenshot_dir: Path,
    *,
    file_system: FileSystem,
    llm: ChatGoogle,
    token_cost: TokenCost,
) -> BatchExperimentRow:
    user_message, context = await _build_prompt_message(spec, screenshot_dir, file_system)
    messages = [_build_system_message(), user_message]
    started = time.perf_counter()
    image_path = screenshot_dir / spec.image_name

    try:
        response = await llm.ainvoke(messages, output_format=BatchVerificationPayload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        parsed = response.completion
        usage = response.usage
        prompt_tokens = int(usage.prompt_tokens) if usage else None
        prompt_image_tokens = (
            int(usage.prompt_image_tokens) if usage and usage.prompt_image_tokens is not None else None
        )
        completion_tokens = int(usage.completion_tokens) if usage else None
        total_tokens = int(usage.total_tokens) if usage else None
        browser_use_cost = None
        if usage:
            calculated = await token_cost.calculate_cost(llm.model, usage)
            browser_use_cost = calculated.total_cost if calculated else None

        observed_by_label = {_normalize_text(field.field_label): field for field in parsed.fields}
        field_results = [
            _evaluate_field(expected, observed_by_label.get(_normalize_text(expected.field_label)))
            for expected in spec.fields
        ]
        fields_correct = sum(1 for field in field_results if field.correct)
        official_cost = _official_cost_usd(prompt_tokens, completion_tokens)
        return BatchExperimentRow(
            page_id=spec.page_id,
            page_label=spec.page_label,
            image_name=spec.image_name,
            image_path=str(image_path),
            field_count=len(spec.fields),
            reasoning_mode="default",
            resize_mode="original",
            browser_use_mode="static_real_workday_viewport",
            model_name=llm.model,
            status="success",
            fields_correct=fields_correct,
            all_fields_correct=fields_correct == len(spec.fields),
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            prompt_image_tokens=prompt_image_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            browser_use_reported_cost_usd=browser_use_cost,
            official_price_cost_usd=official_cost,
            effective_cost_per_field_usd=(official_cost / len(spec.fields)) if official_cost is not None else None,
            screenshot_dimensions=context.screenshot_dimensions,
            field_results=field_results,
            raw_response=parsed.model_dump_json(),
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return BatchExperimentRow(
            page_id=spec.page_id,
            page_label=spec.page_label,
            image_name=spec.image_name,
            image_path=str(image_path),
            field_count=len(spec.fields),
            reasoning_mode="default",
            resize_mode="original",
            browser_use_mode="static_real_workday_viewport",
            model_name=llm.model,
            status="error",
            latency_ms=latency_ms,
            screenshot_dimensions=context.screenshot_dimensions,
            error=str(exc),
        )


def _render_summary_markdown(summary: dict[str, Any], output_dir: Path) -> str:
    lines = [
        "# Real Workday Batch Verification Experiment Summary",
        "",
        f"- Output dir: `{output_dir}`",
        f"- Model: `{summary['model_name']}`",
        f"- Screenshot dir: `{summary['screenshot_dir']}`",
        "- Browser Use cost values use Browser Use's internal pricing table.",
        "- Official cost values use the Gemini Developer API pricing constants configured in this script.",
        "",
        "## Per-call Results",
        "",
    ]
    for row in summary["rows"]:
        lines.append(f"### `{row['page_id']}`")
        lines.append(f"- Screenshot: `{row['image_name']}`")
        lines.append(f"- Fields in call: {row['field_count']}")
        lines.append(f"- Fields correct: {row['fields_correct']}")
        lines.append(f"- All fields correct: {row['all_fields_correct']}")
        lines.append(f"- Latency (ms): {row['latency_ms']}")
        lines.append(f"- Prompt tokens: {row['prompt_tokens']}")
        lines.append(f"- Prompt image tokens: {row['prompt_image_tokens']}")
        lines.append(f"- Completion tokens: {row['completion_tokens']}")
        lines.append(f"- Official cost (USD): {row['official_price_cost_usd']}")
        lines.append(f"- Effective cost per field (USD): {row['effective_cost_per_field_usd']}")
        lines.append("")
    aggregate = summary["aggregate"]
    lines += [
        "## Aggregate",
        "",
        f"- Calls: {aggregate['calls']}",
        f"- Successful calls: {aggregate['successful_calls']}",
        f"- All-fields-correct calls: {aggregate['all_fields_correct_calls']}",
        f"- Total fields: {aggregate['total_fields']}",
        f"- Total correct fields: {aggregate['total_correct_fields']}",
        f"- Field accuracy: {aggregate['field_accuracy']}",
        f"- Avg official cost per call (USD): {aggregate['avg_official_cost_per_call_usd']}",
        f"- Median official cost per call (USD): {aggregate['median_official_cost_per_call_usd']}",
        f"- Effective cost per field (USD): {aggregate['effective_cost_per_field_usd']}",
        f"- Avg latency per call (ms): {aggregate['avg_latency_ms']}",
        "",
        "## Whole-flow rough guess",
        "",
        f"- lower / 4 visual page checks: {aggregate['whole_flow_projection']['lower_4_calls_usd']}",
        f"- mid / 6 visual page checks: {aggregate['whole_flow_projection']['mid_6_calls_usd']}",
        f"- upper / 8 visual page checks: {aggregate['whole_flow_projection']['upper_8_calls_usd']}",
        "",
    ]
    return "\n".join(lines)


def _summarize_rows(rows: list[BatchExperimentRow], screenshot_dir: Path) -> dict[str, Any]:
    successful = [row for row in rows if row.status == "success"]
    official_costs = [row.official_price_cost_usd for row in successful if row.official_price_cost_usd is not None]
    latencies = [row.latency_ms for row in successful if row.latency_ms is not None]
    total_fields = sum(row.field_count for row in rows)
    total_correct_fields = sum(row.fields_correct or 0 for row in rows)
    avg_call_cost = (sum(official_costs) / len(official_costs)) if official_costs else None
    aggregate = {
        "calls": len(rows),
        "successful_calls": len(successful),
        "all_fields_correct_calls": sum(1 for row in successful if row.all_fields_correct),
        "total_fields": total_fields,
        "total_correct_fields": total_correct_fields,
        "field_accuracy": (total_correct_fields / total_fields) if total_fields else None,
        "avg_official_cost_per_call_usd": avg_call_cost,
        "median_official_cost_per_call_usd": statistics.median(official_costs) if official_costs else None,
        "effective_cost_per_field_usd": (
            (sum(official_costs) / total_fields) if official_costs and total_fields else None
        ),
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "whole_flow_projection": (
            {
                "lower_4_calls_usd": avg_call_cost * 4,
                "mid_6_calls_usd": avg_call_cost * 6,
                "upper_8_calls_usd": avg_call_cost * 8,
            }
            if avg_call_cost is not None
            else None
        ),
    }
    return {
        "model_name": DEFAULT_MODEL_NAME,
        "screenshot_dir": str(screenshot_dir),
        "official_pricing": {
            "input_cost_per_million": OFFICIAL_GEMINI_3_FLASH_PREVIEW_INPUT_COST_PER_MILLION,
            "output_cost_per_million": OFFICIAL_GEMINI_3_FLASH_PREVIEW_OUTPUT_COST_PER_MILLION,
        },
        "rows": [row.model_dump(mode="json") for row in rows],
        "aggregate": aggregate,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure real Workday screenshot batch verification cost with Gemini.")
    parser.add_argument(
        "--screenshot-dir",
        type=Path,
        default=DEFAULT_SCREENSHOT_DIR,
        help="Directory containing the real Workday screenshots to evaluate.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / time.strftime("%Y%m%d-%H%M%S"),
        help="Directory where experiment outputs should be written.",
    )
    parser.add_argument(
        "--page-ids",
        nargs="+",
        default=None,
        help="Optional subset of page ids to run.",
    )
    return parser.parse_args()


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing")

    args = _parse_args()
    screenshot_dir = args.screenshot_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_specs = WORKDAY_SCREENSHOT_SPECS
    if args.page_ids:
        wanted = {page_id.strip() for page_id in args.page_ids}
        selected_specs = [spec for spec in WORKDAY_SCREENSHOT_SPECS if spec.page_id in wanted]

    missing_files = [spec.image_name for spec in selected_specs if not (screenshot_dir / spec.image_name).exists()]
    if missing_files:
        raise FileNotFoundError(f"Missing screenshot files: {missing_files}")

    token_cost = TokenCost(include_cost=True)
    await token_cost.initialize()

    llm = ChatGoogle(
        model=DEFAULT_MODEL_NAME,
        api_key=api_key,
        temperature=0,
        max_output_tokens=2048,
        max_retries=1,
    )

    rows: list[BatchExperimentRow] = []
    with tempfile.TemporaryDirectory(prefix="real-workday-batch-verifier-") as temp_dir:
        file_system = FileSystem(Path(temp_dir))
        manifest = [spec.model_dump(mode="json") for spec in selected_specs]
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        for spec in selected_specs:
            row = await _invoke_batch_verifier(
                spec,
                screenshot_dir,
                file_system=file_system,
                llm=llm,
                token_cost=token_cost,
            )
            rows.append(row)
            print(
                json.dumps(
                    {
                        "page_id": row.page_id,
                        "status": row.status,
                        "fields_correct": row.fields_correct,
                        "field_count": row.field_count,
                        "official_price_cost_usd": row.official_price_cost_usd,
                        "latency_ms": row.latency_ms,
                    }
                )
            )

    summary = _summarize_rows(rows, screenshot_dir)
    (output_dir / "results.json").write_text(json.dumps([row.model_dump(mode="json") for row in rows], indent=2))
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (output_dir / "SUMMARY.md").write_text(_render_summary_markdown(summary, output_dir))


if __name__ == "__main__":
    asyncio.run(main())
