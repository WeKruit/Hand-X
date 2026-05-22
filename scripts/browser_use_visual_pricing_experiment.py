"""Measure Browser Use screenshot-path cost for toy Workday visual verification.

This script is intentionally isolated from Hand-X runtime behavior. It reuses
the deterministic toy Workday scenarios from ``visual_bakeoff.py`` and measures
what happens when we push screenshots through Browser Use's own message
serialization path with ``ChatGoogle(model="gemini-3-flash-preview")``.

The goal is to answer a narrower follow-up question than the original bakeoff:

"If we keep Hand-X unchanged for now and prototype the visual layer using
Browser Use screenshot inclusion / resizing plus Gemini 3 Flash Preview, what
does a single verification call cost, and what rough price envelope does that
imply for one full toy application?"
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
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from pydantic import BaseModel
from visual_bakeoff import (
    REPO_ROOT,
    SCENARIOS,
    TOY_WORKDAY_DIR,
    VIEWPORT,
    ScenarioSpec,
    ToyServer,
    VerificationPayload,
    _compare_observed_value,
    render_review,
    seed_full_application,
    settle,
    show_step,
)

from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.views import SerializedDOMState
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.messages import SystemMessage
from browser_use.tokens.service import TokenCost
from browser_use.tools.service import Tools

DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "browser_use_visual_pricing"
OFFICIAL_GEMINI_3_FLASH_PREVIEW_INPUT_COST_PER_MILLION = 0.50
OFFICIAL_GEMINI_3_FLASH_PREVIEW_OUTPUT_COST_PER_MILLION = 3.00
DEFAULT_RESIZE = (1400, 850)


class _PageAdapter:
    """Adapt Browser Use's page wrapper to the minimal Playwright-like API we need."""

    def __init__(self, page: Any) -> None:
        self._page = page

    async def evaluate(self, page_function: str, *args: Any) -> Any:
        return await self._page.evaluate(page_function, *args)

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)


class ScreenshotMode(StrEnum):
    VIEWPORT_NO_RESIZE = "viewport_no_resize"
    VIEWPORT_RESIZED = "viewport_resized"
    ELEMENT_RESIZED = "element_resized"


class ReasoningMode(StrEnum):
    DEFAULT = "default"
    MINIMAL = "minimal"


class ExperimentRow(BaseModel):
    scenario_id: str
    field_label: str
    expected_value: str
    browser_use_mode: str
    screenshot_mode: ScreenshotMode
    resize_mode: str
    reasoning_mode: ReasoningMode
    vision_detail_level: str
    model_name: str
    status: str
    observed_value: str | None = None
    matches_expected: bool | None = None
    correct: bool | None = None
    confidence: float | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    prompt_image_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    browser_use_reported_cost_usd: float | None = None
    official_price_cost_usd: float | None = None
    screenshot_dimensions: str | None = None
    raw_response: str | None = None
    error: str | None = None


def _safe_stem(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value).strip("_") or "item"


def _build_task(scenario: ScenarioSpec) -> str:
    return (
        "This is a visual verification benchmark on a toy Workday-style job application. "
        f'Target field label: "{scenario.field_label}". '
        f'Expected visible value: "{scenario.expected_value}". '
        "Use the screenshot and any provided browser state to determine the value visibly shown for this field. "
        "Return structured output only."
    )


def _build_system_message() -> SystemMessage:
    return SystemMessage(
        content=(
            "You are a careful visual verifier for Workday-style application fields. "
            "Inspect the screenshot and provided state, then return JSON with keys "
            "`observed_value`, `matches_expected`, and `confidence`. "
            "Prefer the value that is visibly shown to the user."
        )
    )


def _official_cost_usd(prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (
        prompt_tokens * OFFICIAL_GEMINI_3_FLASH_PREVIEW_INPUT_COST_PER_MILLION
        + completion_tokens * OFFICIAL_GEMINI_3_FLASH_PREVIEW_OUTPUT_COST_PER_MILLION
    ) / 1_000_000


def _minimal_browser_state(state: BrowserStateSummary) -> BrowserStateSummary:
    return BrowserStateSummary(
        dom_state=SerializedDOMState(_root=None, selector_map={}),
        url=state.url,
        title=state.title,
        tabs=state.tabs,
        screenshot=None,
    )


def _screenshot_dimensions(screenshot_b64: str) -> str:
    from io import BytesIO

    from PIL import Image

    data = base64.b64decode(screenshot_b64)
    with Image.open(BytesIO(data)) as image:
        return f"{image.size[0]}x{image.size[1]}"


def _variant_key(row: ExperimentRow) -> str:
    return f"{row.screenshot_mode.value}__{row.reasoning_mode.value}"


async def _navigate_and_seed(browser_session: BrowserSession, toy_url: str) -> None:
    tools = Tools()
    await tools.navigate(url=toy_url, new_tab=False, browser_session=browser_session)
    page = await browser_session.must_get_current_page()
    adapted_page = _PageAdapter(page)
    await adapted_page.evaluate("() => window.scrollTo(0, 0)")
    seeded_page = cast(Any, adapted_page)
    await seed_full_application(seeded_page)
    await settle(seeded_page)


async def _set_scenario(page: Any, scenario: ScenarioSpec) -> None:
    adapted_page = _PageAdapter(page)
    scenario_page = cast(Any, adapted_page)
    await show_step(scenario_page, scenario.step)
    if scenario.step == 6:
        await render_review(scenario_page)
    await settle(scenario_page)


async def _build_prompt_message(
    browser_state: BrowserStateSummary,
    file_system: FileSystem,
    screenshot_b64: str,
    scenario: ScenarioSpec,
    *,
    llm_screenshot_size: tuple[int, int] | None,
    vision_detail_level: str,
) -> Any:
    prompt = AgentMessagePrompt(
        browser_state_summary=browser_state,
        file_system=file_system,
        task=_build_task(scenario),
        screenshots=[screenshot_b64],
        vision_detail_level=vision_detail_level,  # type: ignore[arg-type]
        llm_screenshot_size=llm_screenshot_size,
    )
    return prompt.get_user_message(use_vision=True)


async def _invoke_verifier(
    scenario: ScenarioSpec,
    screenshot_mode: ScreenshotMode,
    reasoning_mode: ReasoningMode,
    *,
    browser_session: BrowserSession,
    page: Any,
    file_system: FileSystem,
    llm: ChatGoogle,
    token_cost: TokenCost,
    vision_detail_level: str,
    llm_screenshot_size: tuple[int, int] | None,
) -> ExperimentRow:
    state = await browser_session.get_browser_state_summary(include_screenshot=True)
    screenshot_b64: str
    browser_state_for_prompt: BrowserStateSummary
    browser_use_mode: str
    resize_mode = "resized" if llm_screenshot_size else "original"

    if screenshot_mode is ScreenshotMode.VIEWPORT_NO_RESIZE:
        if not state.screenshot:
            raise RuntimeError("Browser state summary did not include a screenshot")
        screenshot_b64 = state.screenshot
        browser_state_for_prompt = state
        browser_use_mode = "default_viewport_state"
        llm_size = None
    elif screenshot_mode is ScreenshotMode.VIEWPORT_RESIZED:
        if not state.screenshot:
            raise RuntimeError("Browser state summary did not include a screenshot")
        screenshot_b64 = state.screenshot
        browser_state_for_prompt = state
        browser_use_mode = "default_viewport_state"
        llm_size = llm_screenshot_size
    else:
        element_bytes = await browser_session.screenshot_element(scenario.selector, format="png")
        screenshot_b64 = base64.b64encode(element_bytes).decode("ascii")
        browser_state_for_prompt = _minimal_browser_state(state)
        browser_use_mode = "deterministic_element_crop"
        llm_size = llm_screenshot_size

    user_message = await _build_prompt_message(
        browser_state_for_prompt,
        file_system,
        screenshot_b64,
        scenario,
        llm_screenshot_size=llm_size,
        vision_detail_level=vision_detail_level,
    )
    messages = [_build_system_message(), user_message]
    started = time.perf_counter()
    try:
        response = await llm.ainvoke(messages, output_format=VerificationPayload)
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
        correct = _compare_observed_value(parsed.observed_value, scenario.expected_value, scenario.comparison_mode)
        return ExperimentRow(
            scenario_id=scenario.scenario_id,
            field_label=scenario.field_label,
            expected_value=scenario.expected_value,
            browser_use_mode=browser_use_mode,
            screenshot_mode=screenshot_mode,
            resize_mode=resize_mode,
            reasoning_mode=reasoning_mode,
            vision_detail_level=vision_detail_level,
            model_name=llm.model,
            status="success",
            observed_value=parsed.observed_value,
            matches_expected=parsed.matches_expected,
            correct=correct,
            confidence=parsed.confidence,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            prompt_image_tokens=prompt_image_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            browser_use_reported_cost_usd=browser_use_cost,
            official_price_cost_usd=_official_cost_usd(prompt_tokens, completion_tokens),
            screenshot_dimensions=_screenshot_dimensions(screenshot_b64),
            raw_response=parsed.model_dump_json(),
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ExperimentRow(
            scenario_id=scenario.scenario_id,
            field_label=scenario.field_label,
            expected_value=scenario.expected_value,
            browser_use_mode=browser_use_mode,
            screenshot_mode=screenshot_mode,
            resize_mode=resize_mode,
            reasoning_mode=reasoning_mode,
            vision_detail_level=vision_detail_level,
            model_name=llm.model,
            status="error",
            latency_ms=latency_ms,
            screenshot_dimensions=_screenshot_dimensions(screenshot_b64),
            error=str(exc),
        )


def _summarize_results(rows: list[ExperimentRow]) -> dict[str, Any]:
    variants: dict[str, list[ExperimentRow]] = {}
    for row in rows:
        variants.setdefault(_variant_key(row), []).append(row)

    by_variant: dict[str, Any] = {}
    for variant, entries in variants.items():
        successes = [entry for entry in entries if entry.status == "success"]
        accuracies = [entry.correct for entry in successes if entry.correct is not None]
        latencies = [entry.latency_ms for entry in entries if entry.latency_ms is not None]
        official_costs = [
            entry.official_price_cost_usd for entry in successes if entry.official_price_cost_usd is not None
        ]
        browser_use_costs = [
            entry.browser_use_reported_cost_usd
            for entry in successes
            if entry.browser_use_reported_cost_usd is not None
        ]
        prompt_tokens = [entry.prompt_tokens for entry in successes if entry.prompt_tokens is not None]
        prompt_image_tokens = [
            entry.prompt_image_tokens for entry in successes if entry.prompt_image_tokens is not None
        ]
        completion_tokens = [entry.completion_tokens for entry in successes if entry.completion_tokens is not None]

        avg_official = (sum(official_costs) / len(official_costs)) if official_costs else None
        avg_latency = (sum(latencies) / len(latencies)) if latencies else None
        median_latency = statistics.median(latencies) if latencies else None
        projection = (
            {
                "low_3_calls_usd": avg_official * 3,
                "mid_6_calls_usd": avg_official * 6,
                "high_11_calls_usd": avg_official * 11,
            }
            if avg_official is not None
            else None
        )
        by_variant[variant] = {
            "calls": len(entries),
            "successful_calls": len(successes),
            "error_calls": len(entries) - len(successes),
            "accuracy": (sum(1 for item in accuracies if item) / len(accuracies)) if accuracies else None,
            "avg_latency_ms": avg_latency,
            "median_latency_ms": median_latency,
            "avg_official_cost_usd": avg_official,
            "avg_browser_use_reported_cost_usd": (
                sum(browser_use_costs) / len(browser_use_costs) if browser_use_costs else None
            ),
            "avg_prompt_tokens": (sum(prompt_tokens) / len(prompt_tokens)) if prompt_tokens else None,
            "avg_prompt_image_tokens": (
                sum(prompt_image_tokens) / len(prompt_image_tokens) if prompt_image_tokens else None
            ),
            "avg_completion_tokens": (sum(completion_tokens) / len(completion_tokens)) if completion_tokens else None,
            "application_cost_projection": projection,
        }

    return {
        "model_name": "gemini-3-flash-preview",
        "official_pricing": {
            "input_cost_per_million": OFFICIAL_GEMINI_3_FLASH_PREVIEW_INPUT_COST_PER_MILLION,
            "output_cost_per_million": OFFICIAL_GEMINI_3_FLASH_PREVIEW_OUTPUT_COST_PER_MILLION,
        },
        "variant_count": len(by_variant),
        "variants": by_variant,
    }


def _render_summary_markdown(summary: dict[str, Any], output_dir: Path) -> str:
    lines = [
        "# Browser Use Visual Pricing Experiment Summary",
        "",
        f"- Output dir: `{output_dir}`",
        f"- Model: `{summary['model_name']}`",
        "- Browser Use cost values use Browser Use's internal pricing table.",
        "- Official cost values use the Gemini Developer API pricing constants configured in this script.",
        "",
        "## Variant Metrics",
        "",
    ]
    for variant, stats in summary["variants"].items():
        lines.append(f"### `{variant}`")
        lines.append(f"- Calls: {stats['calls']}")
        lines.append(f"- Successful calls: {stats['successful_calls']}")
        lines.append(f"- Error calls: {stats['error_calls']}")
        lines.append(f"- Accuracy: {stats['accuracy']}")
        lines.append(f"- Avg latency (ms): {stats['avg_latency_ms']}")
        lines.append(f"- Median latency (ms): {stats['median_latency_ms']}")
        lines.append(f"- Avg official cost (USD): {stats['avg_official_cost_usd']}")
        lines.append(f"- Avg Browser Use reported cost (USD): {stats['avg_browser_use_reported_cost_usd']}")
        lines.append(f"- Avg prompt tokens: {stats['avg_prompt_tokens']}")
        lines.append(f"- Avg prompt image tokens: {stats['avg_prompt_image_tokens']}")
        lines.append(f"- Avg completion tokens: {stats['avg_completion_tokens']}")
        projection = stats["application_cost_projection"]
        if projection:
            lines.append("- Application cost projection:")
            lines.append(f"  - low / 3 calls: {projection['low_3_calls_usd']}")
            lines.append(f"  - mid / 6 calls: {projection['mid_6_calls_usd']}")
            lines.append(f"  - high / 11 calls: {projection['high_11_calls_usd']}")
        lines.append("")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure Browser Use screenshot-path Gemini visual-verification cost.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / time.strftime("%Y%m%d-%H%M%S"),
        help="Directory where experiment results should be written.",
    )
    parser.add_argument(
        "--scenario-limit",
        type=int,
        default=None,
        help="Limit the number of toy scenarios for a faster first pass.",
    )
    parser.add_argument(
        "--vision-detail-level",
        choices=["auto", "low", "high"],
        default="auto",
        help="Browser Use image detail level to attach to the prompt images.",
    )
    parser.add_argument(
        "--resize-width",
        type=int,
        default=DEFAULT_RESIZE[0],
        help="Width for Browser Use screenshot resizing when the resize mode is enabled.",
    )
    parser.add_argument(
        "--resize-height",
        type=int,
        default=DEFAULT_RESIZE[1],
        help="Height for Browser Use screenshot resizing when the resize mode is enabled.",
    )
    parser.add_argument(
        "--screenshot-modes",
        nargs="+",
        choices=[mode.value for mode in ScreenshotMode],
        default=[mode.value for mode in ScreenshotMode],
        help="Subset of Browser Use screenshot modes to evaluate.",
    )
    parser.add_argument(
        "--reasoning-modes",
        nargs="+",
        choices=[mode.value for mode in ReasoningMode],
        default=[mode.value for mode in ReasoningMode],
        help="Subset of Gemini reasoning modes to evaluate.",
    )
    return parser.parse_args()


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing")

    args = _parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    scenario_limit = args.scenario_limit
    scenarios = SCENARIOS[:scenario_limit] if scenario_limit else list(SCENARIOS)
    screenshot_modes = [ScreenshotMode(item) for item in args.screenshot_modes]
    reasoning_modes = [ReasoningMode(item) for item in args.reasoning_modes]
    llm_screenshot_size = (args.resize_width, args.resize_height)

    rows: list[ExperimentRow] = []
    token_cost = TokenCost(include_cost=True)
    await token_cost.initialize()

    with tempfile.TemporaryDirectory(prefix="browser-use-visual-pricing-") as temp_dir:
        file_system = FileSystem(Path(temp_dir))

        with ToyServer(TOY_WORKDAY_DIR) as toy_url:
            browser_session = BrowserSession(
                browser_profile=BrowserProfile(
                    headless=True,
                    user_data_dir=None,
                    keep_alive=True,
                    enable_default_extensions=True,
                    viewport=ViewportSize(width=VIEWPORT["width"], height=VIEWPORT["height"]),
                    device_scale_factor=2,
                )
            )
            await browser_session.start()
            try:
                await _navigate_and_seed(browser_session, toy_url)
                page = await browser_session.must_get_current_page()

                llms = {
                    ReasoningMode.DEFAULT: ChatGoogle(
                        model="gemini-3-flash-preview",
                        api_key=api_key,
                        temperature=0,
                        max_output_tokens=1024,
                        max_retries=1,
                    ),
                    ReasoningMode.MINIMAL: ChatGoogle(
                        model="gemini-3-flash-preview",
                        api_key=api_key,
                        temperature=0,
                        max_output_tokens=512,
                        thinking_level="minimal",
                        max_retries=1,
                    ),
                }

                for scenario in scenarios:
                    await _set_scenario(page, scenario)
                    for screenshot_mode in screenshot_modes:
                        for reasoning_mode in reasoning_modes:
                            llm = llms[reasoning_mode]
                            row = await _invoke_verifier(
                                scenario,
                                screenshot_mode,
                                reasoning_mode,
                                browser_session=browser_session,
                                page=page,
                                file_system=file_system,
                                llm=llm,
                                token_cost=token_cost,
                                vision_detail_level=args.vision_detail_level,
                                llm_screenshot_size=llm_screenshot_size,
                            )
                            rows.append(row)
                            print(
                                json.dumps(
                                    {
                                        "scenario_id": row.scenario_id,
                                        "screenshot_mode": row.screenshot_mode,
                                        "reasoning_mode": row.reasoning_mode,
                                        "status": row.status,
                                        "correct": row.correct,
                                        "official_price_cost_usd": row.official_price_cost_usd,
                                        "latency_ms": row.latency_ms,
                                    }
                                )
                            )
            finally:
                await browser_session.kill()
                await browser_session.event_bus.stop(clear=True, timeout=5)

    summary = _summarize_results(rows)
    (output_dir / "results.json").write_text(json.dumps([row.model_dump(mode="json") for row in rows], indent=2))
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (output_dir / "SUMMARY.md").write_text(_render_summary_markdown(summary, output_dir))


if __name__ == "__main__":
    asyncio.run(main())
