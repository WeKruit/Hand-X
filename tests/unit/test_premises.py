"""Structural invariant tests for the 5 DomHand Premises.

These tests prove the premises ALWAYS hold as invariants — not as
implementation detail checks, but as structural guarantees.

Premise 1: On new page → always DomHand first, with platform strategy dispatch.
Premise 2: domhand_fill returns inline page-ready state; assess_state is optional.
Premise 3: DomHand extracted_content is PURELY informational — no directives.
Premise 4: Broad refill blocked after initial fill; only targeted tools for recovery.
Premise 5: Agent prompt requires visual self-review; DomHand never drives decisions.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

DIRECTIVE_PATTERNS = re.compile(
    r"""
    \b(?:
        STOP               |  # hard stop
        Do\s+NOT            |  # prohibition
        do\s+NOT            |  # prohibition lowercase start
        DO\s+NOT            |  # prohibition all caps
        MUST\s+NOT          |  # prohibition
        NEVER               |  # prohibition
        call\s+domhand_     |  # tool invocation directive
        Run\s+domhand_      |  # tool invocation directive
        Use\s+domhand_      |  # tool invocation directive
        click\s+Next        |  # navigation directive
        click\s+Continue    |  # navigation directive
        click\s+Save        |  # navigation directive
        Advance\s+to        |  # navigation directive
        Proceed             |  # navigation directive
        inspect\s+the       |  # observation directive
        recover\s+the       |  # recovery directive
        re-running          |  # retry directive
        instead\s+of        |  # alternative suggestion
        prefer\s+browser    |  # tool preference directive
        COMPLETE\s+[—–-]    |  # completion directive with dash
        is\s+COMPLETE\.     |  # completion statement as directive
        Section\s+is\s+COMPLETE   # completion directive
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

ALLOWED_PREFIXES = re.compile(r"^DomHand[:\s]")


# ===========================================================================
# PREMISE 3 — DomHand output is PURELY informational, no directives
# ===========================================================================


class TestPremise3NoDirectivesInExtractedContent:
    """Every extracted_content from DomHand tools must be purely informational.

    Structural test: scans ALL return paths of DomHand actions for directive
    language. This catches regressions regardless of which code path runs.
    """

    def _collect_action_result_literals(
        self, filepath: Path, *, fields: tuple[str, ...] = ("extracted_content",)
    ) -> list[tuple[int, str, str]]:
        """Parse a Python file's AST and extract all string literals
        assigned to the given fields in ActionResult(...) calls.
        Returns list of (lineno, field_name, text)."""
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
        results = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name != "ActionResult":
                continue
            for kw in node.keywords:
                if kw.arg not in fields:
                    continue
                literals = self._extract_string_literals(kw.value)
                for lit in literals:
                    results.append((kw.value.lineno, kw.arg, lit))
        return results

    def _extract_string_literals(self, node: ast.AST) -> list[str]:
        """Recursively extract string literals from AST nodes (handles
        f-strings, JoinedStr, concatenation, etc.)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
            return [" ".join(parts)] if parts else []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._extract_string_literals(node.left) + self._extract_string_literals(node.right)
        if isinstance(node, ast.Call):
            return []
        return []

    @pytest.mark.parametrize(
        "action_file",
        [
            "ghosthands/actions/domhand_fill.py",
            "ghosthands/actions/domhand_assess_state.py",
            "ghosthands/actions/domhand_fill_repeaters.py",
            "ghosthands/actions/domhand_select.py",
            "ghosthands/actions/domhand_interact_control.py",
            "ghosthands/actions/domhand_click_button.py",
            "ghosthands/actions/domhand_upload.py",
            "ghosthands/actions/domhand_expand.py",
            "ghosthands/actions/domhand_close_popup.py",
            "ghosthands/actions/domhand_check_agreement.py",
            "ghosthands/actions/domhand_record_expected_value.py",
        ],
    )
    def test_no_directive_language_in_extracted_content_literals(self, action_file: str):
        """Scan every ActionResult(extracted_content=...) literal in DomHand
        action files. None may contain directive/imperative language."""
        root = Path(__file__).resolve().parents[2]
        filepath = root / action_file
        if not filepath.exists():
            pytest.skip(f"{action_file} not found")
        results = self._collect_action_result_literals(filepath)
        violations = []
        for lineno, field, text in results:
            match = DIRECTIVE_PATTERNS.search(text)
            if match:
                violations.append(
                    f"  {action_file}:{lineno} [{field}] — found '{match.group()}' in: {text[:120]}..."
                )
        assert not violations, (
            f"Premise 3 violation: directive language in extracted_content:\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize(
        "action_file",
        [
            "ghosthands/actions/domhand_fill.py",
            "ghosthands/actions/domhand_assess_state.py",
            "ghosthands/actions/domhand_fill_repeaters.py",
            "ghosthands/actions/domhand_select.py",
            "ghosthands/actions/domhand_interact_control.py",
            "ghosthands/actions/domhand_click_button.py",
            "ghosthands/actions/domhand_upload.py",
            "ghosthands/actions/domhand_expand.py",
            "ghosthands/actions/domhand_close_popup.py",
            "ghosthands/actions/domhand_check_agreement.py",
            "ghosthands/actions/domhand_record_expected_value.py",
        ],
    )
    def test_no_directive_language_in_error_literals(self, action_file: str):
        """Scan every ActionResult(error=...) literal in DomHand action files.
        Error messages are agent-visible and must also be directive-free."""
        root = Path(__file__).resolve().parents[2]
        filepath = root / action_file
        if not filepath.exists():
            pytest.skip(f"{action_file} not found")
        results = self._collect_action_result_literals(filepath, fields=("error",))
        violations = []
        for lineno, field, text in results:
            match = DIRECTIVE_PATTERNS.search(text)
            if match:
                violations.append(
                    f"  {action_file}:{lineno} [{field}] — found '{match.group()}' in: {text[:120]}..."
                )
        assert not violations, (
            f"Premise 3 violation: directive language in error= field:\n"
            + "\n".join(violations)
        )

    def test_no_directive_in_service_checkpoint_extracted_content(self):
        """browser_use/agent/service.py checkpoint/guard ActionResults must
        also be directive-free when they set extracted_content."""
        root = Path(__file__).resolve().parents[2]
        filepath = root / "browser_use" / "agent" / "service.py"
        results = self._collect_action_result_literals(filepath)
        violations = []
        for lineno, field, text in results:
            match = DIRECTIVE_PATTERNS.search(text)
            if match:
                violations.append(
                    f"  service.py:{lineno} [{field}] — found '{match.group()}' in: {text[:120]}..."
                )
        assert not violations, (
            f"Premise 3 violation in service.py checkpoint extracted_content:\n"
            + "\n".join(violations)
        )

    def test_no_directive_in_service_error_fields(self):
        """browser_use/agent/service.py error= fields must also be directive-free."""
        root = Path(__file__).resolve().parents[2]
        filepath = root / "browser_use" / "agent" / "service.py"
        results = self._collect_action_result_literals(filepath, fields=("error",))
        violations = []
        for lineno, field, text in results:
            match = DIRECTIVE_PATTERNS.search(text)
            if match:
                violations.append(
                    f"  service.py:{lineno} [{field}] — found '{match.group()}' in: {text[:120]}..."
                )
        assert not violations, (
            f"Premise 3 violation in service.py error= fields:\n"
            + "\n".join(violations)
        )

    def test_build_assess_guidance_note_returns_empty(self):
        """_build_assess_guidance_note must return '' — it must never inject
        directives into the agent's context."""
        from browser_use.agent.message_manager.service import MessageManager
        from browser_use.agent.views import ActionResult
        from browser_use.llm.messages import SystemMessage

        mm = MessageManager.__new__(MessageManager)
        result = [
            ActionResult(
                extracted_content="DomHand assess_state: state=advance; advance_allowed=yes.",
                metadata={
                    "tool": "domhand_assess_state",
                    "application_state_json": {
                        "advance_allowed": True,
                        "terminal_state": "advance",
                    },
                },
            )
        ]
        guidance = mm._build_assess_guidance_note(result)
        assert guidance == "", f"_build_assess_guidance_note must return empty string, got: {guidance!r}"


# ===========================================================================
# PREMISE 3 — Runtime verification (complement to static scan)
# ===========================================================================


class TestPremise3RuntimeOutputs:
    """Run DomHand actions through representative code paths and verify
    extracted_content never contains directives."""

    @pytest.mark.asyncio
    async def test_domhand_fill_success_output_is_informational(self):
        """A successful domhand_fill returns only factual content."""
        from ghosthands.actions.domhand_fill import domhand_fill
        from ghosthands.actions.views import DomHandFillParams, FormField

        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=None)
        browser_session = AsyncMock()
        browser_session.get_current_page = AsyncMock(return_value=page)
        browser_session._gh_last_application_state = None
        browser_session._gh_last_domhand_fill = None

        field = FormField(
            field_id="name-field", name="Full Name", field_type="text", section="Personal"
        )

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"full_name": "Jane Doe"}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={"full_name": "Jane Doe"}),
            patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/apply")),
            patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="page-1")),
            patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
            patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
            patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
            patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
            patch(
                "ghosthands.actions.domhand_fill._attempt_domhand_fill_with_retry_cap",
                AsyncMock(return_value=(True, None, None, 1.0, "Jane Doe")),
            ),
            patch("ghosthands.actions.domhand_fill._record_expected_value_if_settled", AsyncMock(return_value=True)),
            patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
        ):
            result = await domhand_fill(DomHandFillParams(), browser_session)

        content = result.extracted_content or ""
        assert content, "Expected non-empty extracted_content"
        match = DIRECTIVE_PATTERNS.search(content)
        assert match is None, f"Premise 3: directive '{match.group()}' in fill output: {content}"

    @pytest.mark.asyncio
    async def test_domhand_fill_guard_output_is_informational(self):
        """The same-page fill guard returns only factual content."""
        from ghosthands.actions.domhand_fill import domhand_fill
        from ghosthands.actions.views import DomHandFillParams

        page = AsyncMock()
        browser_session = AsyncMock()
        browser_session.get_current_page = AsyncMock(return_value=page)
        browser_session._gh_last_application_state = None
        browser_session._gh_last_domhand_fill = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply",
            "broad_fill_completed": True,
        }

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
            patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/apply")),
            patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="page-1")),
        ):
            result = await domhand_fill(DomHandFillParams(target_section="Info"), browser_session)

        content = result.extracted_content or ""
        match = DIRECTIVE_PATTERNS.search(content)
        assert match is None, f"Premise 3: directive '{match.group()}' in guard output: {content}"

    @pytest.mark.asyncio
    async def test_domhand_fill_advance_guard_output_is_informational(self):
        """The advance guard returns only factual content."""
        from ghosthands.actions.domhand_fill import domhand_fill
        from ghosthands.actions.views import DomHandFillParams

        page = AsyncMock()
        # __ff injection + field extraction return empty (no fields on mock page)
        page.evaluate = AsyncMock(return_value="[]")
        browser_session = AsyncMock()
        browser_session.get_current_page = AsyncMock(return_value=page)
        browser_session._gh_last_application_state = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply",
            "advance_allowed": True,
            "unresolved_required_count": 0,
            "optional_validation_count": 0,
            "visible_error_count": 0,
            "mismatched_count": 0,
            "opaque_count": 0,
            "unverified_count": 0,
        }
        # Same-page field IDs so the SPA guard doesn't open the gate
        browser_session._gh_last_domhand_fill = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply",
            "broad_fill_completed": True,
            "field_ids": ["ff-1", "ff-2"],
        }

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
            patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/apply")),
            patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="page-1")),
        ):
            result = await domhand_fill(DomHandFillParams(target_section="Info"), browser_session)

        content = result.extracted_content or ""
        match = DIRECTIVE_PATTERNS.search(content)
        assert match is None, f"Premise 3: directive '{match.group()}' in advance guard output: {content}"


# ===========================================================================
# PREMISE 3 — Read-state suppression
# ===========================================================================


class TestPremise3ReadStateSuppression:
    """DomHand tool outputs must never appear in the agent's read_state."""

    def test_domhand_tools_are_in_suppression_set(self):
        from browser_use.agent.message_manager.service import _READ_STATE_SUPPRESSED_TOOLS

        assert "domhand_fill" in _READ_STATE_SUPPRESSED_TOOLS
        assert "domhand_assess_state" in _READ_STATE_SUPPRESSED_TOOLS


# ===========================================================================
# PREMISE 4 — Broad refill blocked; only targeted tools for recovery
# ===========================================================================


class TestPremise4BroadRefillBlocked:
    """After one broad domhand_fill, repeated broad fills are blocked.
    Scoped fills (heading_boundary, focus_fields, entry_data) pass through."""

    @pytest.mark.parametrize(
        "params,should_block",
        [
            ({}, True),
            ({"target_section": "Personal Info"}, True),
            ({"heading_boundary": "Education 1"}, False),
            ({"focus_fields": ["field-1"]}, False),
            ({"entry_data": {"school": "MIT"}}, False),
        ],
        ids=["bare_broad", "section_only_is_broad", "heading_passes", "focus_passes", "entry_data_passes"],
    )
    def test_fill_guard_matrix(self, params: dict, should_block: bool):
        """Table-driven: all param shapes vs fill guard."""
        from ghosthands.actions.domhand_fill import _same_page_fill_guard_error

        browser_session = AsyncMock()
        browser_session._gh_last_domhand_fill = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply",
            "broad_fill_completed": True,
        }

        result = _same_page_fill_guard_error(
            browser_session,
            page_context_key="page-1",
            page_url="https://example.com/apply",
            heading_boundary=params.get("heading_boundary"),
            focus_fields=params.get("focus_fields"),
            entry_data=params.get("entry_data"),
        )
        if should_block:
            assert result is not None, f"Expected guard to BLOCK for params={params}"
            match = DIRECTIVE_PATTERNS.search(result)
            assert match is None, f"Guard message contains directive: {result}"
        else:
            assert result is None, f"Expected guard to ALLOW for params={params}"

    @pytest.mark.parametrize(
        "params,should_block",
        [
            ({"action": "domhand_fill", "params": {}}, True),
            ({"action": "domhand_fill", "params": {"target_section": "X"}}, True),
            ({"action": "domhand_fill", "params": {"heading_boundary": "Edu 1"}}, False),
            ({"action": "domhand_fill", "params": {"focus_fields": ["f1"]}}, False),
            ({"action": "domhand_fill", "params": {"entry_data": {"k": "v"}}}, False),
            ({"action": "domhand_select", "params": {}}, False),
            ({"action": "domhand_interact_control", "params": {}}, False),
            ({"action": "input", "params": {}}, False),
        ],
        ids=[
            "bare_fill", "section_fill", "heading_fill", "focus_fill", "entry_fill",
            "select_ok", "interact_ok", "input_ok",
        ],
    )
    def test_checkpoint_decision_matrix(self, params: dict, should_block: bool):
        """Table-driven: fill checkpoint decision for all action shapes."""
        from browser_use.agent.service import _same_page_fill_checkpoint_decision

        last_fill = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply",
            "broad_fill_completed": True,
        }
        decision = _same_page_fill_checkpoint_decision(
            last_fill, [params], current_url="https://example.com/apply"
        )
        if should_block:
            assert decision is not None, f"Expected checkpoint to BLOCK: {params}"
        else:
            assert decision is None, f"Expected checkpoint to ALLOW: {params}"

    @pytest.mark.parametrize(
        "params,should_block",
        [
            ({"action": "domhand_fill", "params": {}}, True),
            ({"action": "domhand_fill", "params": {"heading_boundary": "Edu 1"}}, False),
            ({"action": "domhand_select", "params": {}}, False),
        ],
        ids=["broad_fill_blocked", "scoped_allowed", "other_tool_allowed"],
    )
    def test_advance_decision_matrix(self, params: dict, should_block: bool):
        """After advance_allowed=yes, broad fills blocked, scoped/other allowed."""
        from browser_use.agent.service import _same_page_advance_decision

        last_state = {
            "advance_allowed": True,
            "unresolved_required_count": 0,
            "optional_validation_count": 0,
            "visible_error_count": 0,
            "opaque_count": 0,
        }
        decision = _same_page_advance_decision(last_state, [params])
        if should_block:
            assert decision is not None, f"Expected advance guard to BLOCK: {params}"
        else:
            assert decision is None, f"Expected advance guard to ALLOW: {params}"


# ===========================================================================
# PREMISE 4 — Guard outputs are also directive-free
# ===========================================================================


class TestPremise4GuardOutputsClean:
    """All guard/checkpoint decision messages must be informational."""

    def test_fill_checkpoint_message_is_informational(self):
        from browser_use.agent.service import _same_page_fill_checkpoint_decision

        decision = _same_page_fill_checkpoint_decision(
            {"page_context_key": "p1", "page_url": "https://example.com/apply", "broad_fill_completed": True},
            [{"action": "domhand_fill", "params": {}}],
            current_url="https://example.com/apply",
        )
        assert decision is not None, "Decision should fire for same-page broad fill"
        msg = decision.get("message", "")
        match = DIRECTIVE_PATTERNS.search(msg)
        assert match is None, f"Fill checkpoint message contains directive: {msg}"

    def test_advance_decision_message_is_informational(self):
        from browser_use.agent.service import _same_page_advance_decision

        decision = _same_page_advance_decision(
            {
                "advance_allowed": True,
                "unresolved_required_count": 0,
                "optional_validation_count": 0,
                "visible_error_count": 0,
                "opaque_count": 0,
            },
            [{"action": "domhand_fill", "params": {}}],
        )
        msg = decision.get("message", "")
        match = DIRECTIVE_PATTERNS.search(msg)
        assert match is None, f"Advance decision message contains directive: {msg}"

    def test_blocker_guard_broad_refill_message_is_informational(self):
        from browser_use.agent.service import _blocker_guard_decision

        decision = _blocker_guard_decision(
            {
                "advance_allowed": False,
                "unresolved_required_count": 1,
                "optional_validation_count": 0,
                "visible_error_count": 0,
                "blocking_field_keys": ["field-1"],
                "single_active_blocker": {"field_id": "field-1", "field_label": "Email"},
            },
            [{"action": "domhand_fill", "params": {}}],
        )
        assert decision is not None
        msg = decision.get("message", "")
        match = DIRECTIVE_PATTERNS.search(msg)
        assert match is None, f"Blocker guard message contains directive: {msg}"


# ===========================================================================
# PREMISE 1 — DomHand first on new page, platform strategy dispatch
# ===========================================================================


class TestPremise1DomHandFirstAndPlatformDispatch:
    """Agent prompts must prescribe domhand_fill as the first action on form pages,
    and platform-specific strategy dispatch must exist."""

    REPRESENTATIVE_URLS = [
        ("https://example.wd1.myworkdayjobs.com/en-US/job/123/apply", "workday"),
        ("https://boards.greenhouse.io/company/jobs/123", "greenhouse"),
        ("https://jobs.lever.co/company/123/apply", "lever"),
        ("https://hdpc.fa.us2.oraclecloud.com/fscmUI/faces/apply", "oracle"),
        ("https://generic-ats.com/apply", "generic"),
    ]

    @pytest.mark.parametrize("url,platform", REPRESENTATIVE_URLS, ids=[p for _, p in REPRESENTATIVE_URLS])
    def test_task_prompt_prescribes_domhand_fill_first(self, url: str, platform: str):
        """For every platform, the task prompt must instruct domhand_fill as first action."""
        from ghosthands.agent.prompts import build_task_prompt

        prompt = build_task_prompt(url, "/tmp/resume.pdf", {"email": "a@b.com", "password": "x"})
        assert "domhand_fill" in prompt, f"Task prompt for {platform} must mention domhand_fill"
        assert "On each new page" in prompt or "first action" in prompt.lower(), (
            f"Task prompt for {platform} must prescribe DomHand as first action on new pages"
        )

    @pytest.mark.parametrize("url,platform", REPRESENTATIVE_URLS, ids=[p for _, p in REPRESENTATIVE_URLS])
    def test_system_prompt_includes_domhand_hierarchy(self, url: str, platform: str):
        """System prompt must include the DomHand action hierarchy for all platforms."""
        from ghosthands.agent.prompts import build_system_prompt

        prompt = build_system_prompt({}, url)
        assert "domhand_fill" in prompt, f"System prompt for {platform} must list domhand_fill"

    def test_repeater_strategy_exists_as_separate_action(self):
        """Repeater strategy must exist as a distinct action for multi-entry sections."""
        from ghosthands.actions import domhand_fill_repeaters

        assert hasattr(domhand_fill_repeaters, "domhand_fill_repeaters"), (
            "domhand_fill_repeaters action must exist for repeater strategy dispatch"
        )


# ===========================================================================
# PREMISE 2 — domhand_fill returns inline state; assess optional
# ===========================================================================


class TestPremise2InlineStateFromFill:
    """domhand_fill must return structured metadata with page-readiness signals,
    making assess_state optional for most workflows."""

    @pytest.mark.asyncio
    async def test_fill_metadata_contains_state_signals(self):
        """domhand_fill result metadata must include structured summary with
        counts that would otherwise require assess_state."""
        from ghosthands.actions.domhand_fill import domhand_fill
        from ghosthands.actions.views import DomHandFillParams, FormField

        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=None)
        browser_session = AsyncMock()
        browser_session.get_current_page = AsyncMock(return_value=page)
        browser_session._gh_last_application_state = None
        browser_session._gh_last_domhand_fill = None

        field = FormField(
            field_id="name", name="Full Name", field_type="text", section="Info", required=True
        )

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"full_name": "Jane Doe"}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={"full_name": "Jane Doe"}),
            patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com")),
            patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="page-1")),
            patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
            patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
            patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
            patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
            patch(
                "ghosthands.actions.domhand_fill._attempt_domhand_fill_with_retry_cap",
                AsyncMock(return_value=(True, None, None, 1.0, "Jane Doe")),
            ),
            patch("ghosthands.actions.domhand_fill._record_expected_value_if_settled", AsyncMock(return_value=True)),
            patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
        ):
            result = await domhand_fill(DomHandFillParams(), browser_session)

        meta = result.metadata or {}
        full_json = meta.get("domhand_fill_full_json")
        assert full_json is not None, "domhand_fill must return domhand_fill_full_json in metadata"
        required_keys = {"filled_count", "already_filled_count", "dom_failure_count", "unresolved_required_fields"}
        missing = required_keys - set(full_json.keys())
        assert not missing, f"domhand_fill_full_json missing state keys: {missing}"

    def test_assess_state_is_demoted_in_system_prompt_hierarchy(self):
        """In the system prompt action hierarchy, assess_state must appear
        AFTER domhand_fill and targeted tools (interact_control, select)."""
        from ghosthands.agent.prompts import build_system_prompt

        prompt = build_system_prompt(
            {},
            "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        )
        fill_pos = prompt.find("domhand_fill")
        interact_pos = prompt.find("domhand_interact_control")
        select_pos = prompt.find("domhand_select")
        assess_pos = prompt.find("domhand_assess_state")
        assert fill_pos >= 0, "System prompt must mention domhand_fill"
        assert assess_pos >= 0, "System prompt must mention domhand_assess_state"
        assert fill_pos < assess_pos, "domhand_fill must appear before assess_state in hierarchy"
        if interact_pos > 0:
            assert interact_pos < assess_pos, "interact_control must appear before assess_state"
        if select_pos > 0:
            assert select_pos < assess_pos, "domhand_select must appear before assess_state"

    def test_assess_state_not_required_in_task_prompt_flow(self):
        """Task prompt must NOT mandate calling domhand_assess_state as a
        required step. It's optional per Premise 2."""
        from ghosthands.agent.prompts import build_task_prompt

        prompt = build_task_prompt(
            "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
            "/tmp/resume.pdf",
            {"email": "a@b.com", "password": "x"},
        )
        mandatory_patterns = [
            "must call domhand_assess_state",
            "always call domhand_assess_state",
            "required: domhand_assess_state",
        ]
        for pat in mandatory_patterns:
            assert pat.lower() not in prompt.lower(), (
                f"Premise 2: task prompt must NOT mandate assess_state, found: '{pat}'"
            )


# ===========================================================================
# PREMISE 5 — Agent prompt requires visual self-review
# ===========================================================================


class TestPremise5VisualSelfReview:
    """Agent prompts must instruct visual page review (screenshot) after
    domhand_fill. DomHand output must never prescribe next steps."""

    REPRESENTATIVE_URLS = [
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "https://boards.greenhouse.io/company/jobs/123",
        "https://hdpc.fa.us2.oraclecloud.com/fscmUI/faces/apply",
    ]

    @pytest.mark.parametrize("url", REPRESENTATIVE_URLS)
    def test_task_prompt_requires_visual_review_after_fill(self, url: str):
        """Task prompt must contain visual self-review instructions."""
        from ghosthands.agent.prompts import build_task_prompt

        prompt = build_task_prompt(url, "/tmp/resume.pdf", {"email": "a@b.com", "password": "x"})
        visual_keywords = ["screenshot", "visually", "REVIEW THE PAGE YOURSELF", "visual"]
        has_visual = any(kw.lower() in prompt.lower() for kw in visual_keywords)
        assert has_visual, f"Task prompt for {url} must require visual self-review after domhand_fill"

    @pytest.mark.parametrize("url", REPRESENTATIVE_URLS)
    def test_task_prompt_does_not_defer_decisions_to_domhand(self, url: str):
        """Task prompt must NOT tell the agent to follow DomHand's instructions."""
        from ghosthands.agent.prompts import build_task_prompt

        prompt = build_task_prompt(url, "/tmp/resume.pdf", {"email": "a@b.com", "password": "x"})
        bad_patterns = [
            "follow DomHand",
            "DomHand will tell you",
            "DomHand decides",
            "do what DomHand says",
            "DomHand instructs",
        ]
        for pat in bad_patterns:
            assert pat.lower() not in prompt.lower(), (
                f"Premise 5 violation: prompt defers decisions to DomHand with '{pat}'"
            )

    def test_system_prompt_contains_self_review_rule(self):
        """System prompt must have a structural rule about visual self-review."""
        from ghosthands.agent.prompts import build_system_prompt

        prompt = build_system_prompt({}, "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply")
        visual_phrases = [
            "REVIEW THE PAGE YOURSELF",
            "Your own page observation decides",
            "not DomHand",
        ]
        found = sum(1 for phrase in visual_phrases if phrase in prompt)
        assert found >= 1, (
            "System prompt must contain at least one visual self-review structural rule. "
            f"Checked: {visual_phrases}"
        )


# ===========================================================================
# PREMISE 3+4+5 CROSS-CUT — Service.py guard outputs are all clean
# ===========================================================================


class TestServiceGuardsArePurelyInformational:
    """Every extracted_content emitted by service.py guards must pass the
    same directive scan as DomHand actions themselves."""

    def test_fill_checkpoint_extracted_content(self):
        from browser_use.agent.service import _same_page_fill_checkpoint_decision

        decision = _same_page_fill_checkpoint_decision(
            {"page_context_key": "p", "page_url": "https://example.com/apply", "broad_fill_completed": True},
            [{"action": "domhand_fill", "params": {}}],
            current_url="https://example.com/apply",
        )
        assert decision is not None
        msg = decision["message"]
        match = DIRECTIVE_PATTERNS.search(msg)
        assert match is None, f"Directive in fill checkpoint message: {msg}"

    def test_blocker_guard_extracted_content(self):
        from browser_use.agent.service import _blocker_guard_decision

        decision = _blocker_guard_decision(
            {
                "advance_allowed": False,
                "unresolved_required_count": 1,
                "optional_validation_count": 0,
                "visible_error_count": 0,
                "blocking_field_keys": ["f1"],
                "single_active_blocker": {"field_id": "f1", "field_label": "Email"},
            },
            [{"action": "domhand_fill", "params": {}}],
        )
        assert decision is not None
        msg = decision["message"]
        match = DIRECTIVE_PATTERNS.search(msg)
        assert match is None, f"Directive in blocker guard message: {msg}"

    def test_blocker_guard_same_strategy_message(self):
        from browser_use.agent.service import _blocker_guard_decision

        decision = _blocker_guard_decision(
            {
                "advance_allowed": False,
                "unresolved_required_count": 1,
                "optional_validation_count": 0,
                "visible_error_count": 0,
                "blocking_field_keys": ["f1"],
                "single_active_blocker": {"field_id": "f1", "field_label": "Email", "field_key": "f1"},
                "blocking_field_state_changes": {"f1": "no_state_change"},
            },
            [{"action": "domhand_interact_control", "params": {"field_id": "f1"}}],
            attempt_state={"f1": {"last_attempt_strategy": "domhand_interact_control"}},
        )
        if decision is not None:
            msg = decision.get("message", "")
            match = DIRECTIVE_PATTERNS.search(msg)
            assert match is None, f"Directive in same-strategy guard message: {msg}"


# ===========================================================================
# REVIEW FIX VERIFICATION — Tests for issues found in multi-AI review
# ===========================================================================


class TestReviewFixMF1NoGlobalState:
    """MF1: _COMPLETED_SCOPED_FILLS must NOT exist as module-level global."""

    def test_no_module_level_scoped_fills_global(self):
        import ghosthands.actions.domhand_fill as mod

        assert not hasattr(mod, "_COMPLETED_SCOPED_FILLS"), (
            "MF1 violation: _COMPLETED_SCOPED_FILLS must not be a module-level global. "
            "Scoped fill state should live on browser_session."
        )

    def test_no_module_level_scoped_page_global(self):
        import ghosthands.actions.domhand_fill as mod

        assert not hasattr(mod, "_COMPLETED_SCOPED_PAGE"), (
            "MF1 violation: _COMPLETED_SCOPED_PAGE must not be a module-level global."
        )


class TestReviewFixSF4RenameCheckpoint:
    """SF4: requires_assess_checkpoint must be renamed to broad_fill_completed."""

    def test_no_requires_assess_checkpoint_in_source(self):
        root = Path(__file__).resolve().parents[2]
        source_files = [
            root / "ghosthands" / "actions" / "domhand_fill.py",
            root / "ghosthands" / "actions" / "domhand_assess_state.py",
            root / "browser_use" / "agent" / "service.py",
        ]
        for filepath in source_files:
            content = filepath.read_text()
            assert "requires_assess_checkpoint" not in content, (
                f"SF4 violation: {filepath.name} still uses 'requires_assess_checkpoint'. "
                "Must be renamed to 'broad_fill_completed'."
            )


class TestReviewFixSF6PageScopedGuards:
    """SF6: Fill checkpoint must not fire for a different page than the fill."""

    def test_checkpoint_skips_when_page_url_changed(self):
        from browser_use.agent.service import _same_page_fill_checkpoint_decision

        last_fill = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply/step1",
            "broad_fill_completed": True,
        }
        decision = _same_page_fill_checkpoint_decision(
            last_fill,
            [{"action": "domhand_fill", "params": {}}],
            current_url="https://example.com/apply/step2",
        )
        assert decision is None, (
            "SF6 violation: fill checkpoint fired for a DIFFERENT page URL. "
            "Stale state from page 1 must not block first fill on page 2."
        )

    def test_checkpoint_fires_for_same_page_url(self):
        from browser_use.agent.service import _same_page_fill_checkpoint_decision

        last_fill = {
            "page_context_key": "page-1",
            "page_url": "https://example.com/apply/step1",
            "broad_fill_completed": True,
        }
        decision = _same_page_fill_checkpoint_decision(
            last_fill,
            [{"action": "domhand_fill", "params": {}}],
            current_url="https://example.com/apply/step1",
        )
        assert decision is not None, "Checkpoint should fire when URL matches"


class TestReviewFixSF8OracleComboboxNoFallback:
    """SF8: Oracle combobox JS must not fall back to first visible option."""

    def test_oracle_combobox_js_returns_no_match_on_miss(self):
        root = Path(__file__).resolve().parents[2]
        js_file = root / "ghosthands" / "dom" / "fill_executor.py"
        content = js_file.read_text()
        assert "visible[0]" not in content or "no_match" in content, (
            "SF8 violation: Oracle combobox JS falls back to first visible option "
            "when no match found. Must return {clicked: false, reason: 'no_match'}."
        )


class TestReviewFixSF9AgentSummaryCountsRestored:
    """SF9: Agent-facing summary must include dom_failure_count and unfilled_count."""

    def test_structured_summary_agent_has_diagnostic_counts(self):
        from ghosthands.actions.domhand_fill import domhand_fill  # noqa: F401

        root = Path(__file__).resolve().parents[2]
        source = (root / "ghosthands" / "actions" / "domhand_fill.py").read_text()
        assert '"dom_failure_count"' in source or "'dom_failure_count'" in source, (
            "SF9 violation: structured_summary_agent missing dom_failure_count"
        )
        assert '"unfilled_count"' in source or "'unfilled_count'" in source, (
            "SF9 violation: structured_summary_agent missing unfilled_count"
        )


class TestReviewFixSF7MetadataDirectives:
    """SF7: recommended_next_action metadata must use factual identifiers only."""

    ALLOWED_VALUES = {"review_page_visually", "continue_current_recovery"}

    @pytest.mark.parametrize(
        "action_file",
        [
            "ghosthands/actions/domhand_fill.py",
            "ghosthands/actions/domhand_interact_control.py",
            "ghosthands/actions/domhand_select.py",
            "ghosthands/actions/domhand_record_expected_value.py",
        ],
    )
    def test_recommended_next_action_uses_factual_values(self, action_file: str):
        """All recommended_next_action values must be from the allowed set."""
        root = Path(__file__).resolve().parents[2]
        filepath = root / action_file
        if not filepath.exists():
            pytest.skip(f"{action_file} not found")
        content = filepath.read_text()
        import re as _re

        matches = _re.findall(
            r'"recommended_next_action"[:\s]+"([^"]+)"', content
        )
        matches += _re.findall(
            r"recommended_next_action=['\"]([^'\"]+)['\"]", content
        )
        violations = []
        for val in matches:
            if val not in self.ALLOWED_VALUES:
                violations.append(val)
        assert not violations, (
            f"SF7 violation in {action_file}: recommended_next_action values "
            f"outside allowed set {self.ALLOWED_VALUES}: {violations}"
        )
