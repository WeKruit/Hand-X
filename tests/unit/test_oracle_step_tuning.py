"""Unit tests for Oracle education / focus heuristics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ghosthands.agent.oracle_step_tuning import (
    assess_state_section_suggests_education,
    education_workflow_visible_text_heuristic,
    is_oracle_cloud_job_url,
    maybe_tighten_max_actions_for_oracle_focus,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://hdpc.fa.us2.oraclecloud.com/hcm/", True),
        ("https://example.com/", False),
        ("", False),
    ],
)
def test_is_oracle_cloud_job_url(url: str, expected: bool) -> None:
    assert is_oracle_cloud_job_url(url) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("College / University\nName of school", True),
        ("Cumulative GPA", True),
        ("Add Education", True),
        ("Contact Information only", False),
        ("", False),
    ],
)
def test_education_workflow_visible_text_heuristic(text: str, expected: bool) -> None:
    assert education_workflow_visible_text_heuristic(text) is expected


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("Education and Qualifications", True),
        ("Contact Information", False),
        ("", False),
    ],
)
def test_assess_state_section_suggests_education(section: str, expected: bool) -> None:
    assert assess_state_section_suggests_education(section) is expected


@pytest.mark.asyncio
async def test_maybe_tighten_sets_one_on_oracle_education_text() -> None:
    page = MagicMock()
    page.get_url = AsyncMock(return_value="https://x.oraclecloud.com/apply")
    page.evaluate = AsyncMock(return_value="College / University\nSchool or University")

    browser_session = MagicMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session._gh_last_application_state = None

    agent = SimpleNamespace(
        settings=SimpleNamespace(max_actions_per_step=2),
        browser_session=browser_session,
    )

    await maybe_tighten_max_actions_for_oracle_focus(agent)

    assert agent.settings.max_actions_per_step == 1
    assert agent._gh_baseline_max_actions == 2


@pytest.mark.asyncio
async def test_maybe_tighten_restores_baseline_off_oracle() -> None:
    page = MagicMock()
    page.get_url = AsyncMock(return_value="https://greenhouse.io/jobs")

    browser_session = MagicMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    agent = SimpleNamespace(
        settings=SimpleNamespace(max_actions_per_step=2),
        browser_session=browser_session,
    )

    await maybe_tighten_max_actions_for_oracle_focus(agent)

    assert agent.settings.max_actions_per_step == 2
