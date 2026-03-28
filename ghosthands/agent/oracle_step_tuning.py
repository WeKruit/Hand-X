"""Oracle HCM: slow browser-use action packing on education / autocomplete-heavy pages.

Oracle searchable comboboxes (school, GPA, employer) lose focus when multiple
``input``/``click`` actions run in one agent step.  Step hooks call
``maybe_tighten_max_actions_for_oracle_focus`` so ``max_actions_per_step`` is
temporarily ``1`` only when the URL and page heuristics match, then restored
elsewhere on the same flow.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)

_ORACLE_HOST = re.compile(r"oraclecloud\.com\b", re.I)
_EDUCATION_HEURISTIC = re.compile(
    r"(cumulative\s+gpa|college\s*/\s*university|add\s+education|"
    r"school\s+or\s+university|name\s+of\s+latest\s+employer|"
    r"education\s+and\s+qualifications|university\s+attended)",
    re.I,
)
_SECTION_HINT = re.compile(r"education|college|university|qualification", re.I)


def is_oracle_cloud_job_url(url: str) -> bool:
    return bool(url and _ORACLE_HOST.search(url))


def education_workflow_visible_text_heuristic(text: str) -> bool:
    if not text:
        return False
    return bool(_EDUCATION_HEURISTIC.search(text[:20000]))


def assess_state_section_suggests_education(section: str) -> bool:
    return bool(section and _SECTION_HINT.search(section))


async def maybe_tighten_max_actions_for_oracle_focus(agent: Agent) -> None:
    """Set ``max_actions_per_step`` to 1 on Oracle education/autocomplete pages."""
    if not hasattr(agent, "_gh_baseline_max_actions"):
        agent._gh_baseline_max_actions = int(agent.settings.max_actions_per_step)
    baseline = int(agent._gh_baseline_max_actions)

    try:
        browser_session = getattr(agent, "browser_session", None)
        if browser_session is None:
            agent.settings.max_actions_per_step = baseline
            return

        current_url = ""
        try:
            page = await browser_session.get_current_page()
            if page is not None:
                current_url = await page.get_url()
        except Exception:
            current_url = ""

        if not is_oracle_cloud_job_url(current_url):
            agent.settings.max_actions_per_step = baseline
            return

        want_one = False
        last_state = getattr(browser_session, "_gh_last_application_state", None)
        if isinstance(last_state, dict):
            section = str(last_state.get("current_section") or "")
            if assess_state_section_suggests_education(section):
                want_one = True
                logger.debug(
                    "oracle.focus_mode",
                    extra={"reason": "assess_section", "section": section[:120]},
                )

        if not want_one:
            try:
                page = await browser_session.get_current_page()
                body_snippet = ""
                if page is not None:
                    raw = await page.evaluate(
                        "() => (document.body && document.body.innerText || '').slice(0, 16000)"
                    )
                    body_snippet = raw if isinstance(raw, str) else ""
                if education_workflow_visible_text_heuristic(body_snippet):
                    want_one = True
                    logger.debug("oracle.focus_mode", extra={"reason": "page_text"})
            except Exception:
                pass

        agent.settings.max_actions_per_step = 1 if want_one else baseline
    except Exception:
        agent.settings.max_actions_per_step = baseline
