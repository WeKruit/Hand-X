"""Step hooks for the GhostHands agent.

browser-use's ``Agent.run()`` accepts two optional hook callbacks:

- ``on_step_start(agent: Agent) -> None``  — called before each step
- ``on_step_end(agent: Agent) -> None``    — called after each step

Both receive the ``Agent`` instance (the ``AgentHookFunc`` type alias is
``Callable[[Agent], Awaitable[None]]``).  From the agent you can inspect
``agent.state`` (AgentState), ``agent.history`` (AgentHistoryList), and
``agent.token_cost_service`` for usage data.

These hooks are used to:
- Check for external stop / pause signals (HITL)
- Track cumulative LLM cost against the per-job budget
- Detect blockers reported by the agent's output
- Log step progress for observability
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from browser_use.agent.service import Agent

from ghosthands.agent.oracle_step_tuning import maybe_tighten_max_actions_for_oracle_focus
from ghosthands.cost_summary import build_cost_summary, get_stagehand_usage
from ghosthands.step_trace import (
    attach_step_trace_context,
    publish_browser_session_trace,
)

logger = logging.getLogger(__name__)
_SAME_TAB_GUARD_INSTALLED: set[int] = set()
_FINAL_SUBMIT_GUARD_INSTALLED: set[int] = set()

_SAME_TAB_GUARD_JS = r"""(() => {
	if (window.__ghSameTabGuardInstalled) {
		try {
			document.querySelectorAll('a[target], area[target], form[target]').forEach((el) => el.removeAttribute('target'));
		} catch (e) {}
		return;
	}
	window.__ghSameTabGuardInstalled = true;

	function stripTargets(root) {
		var scope = root && root.querySelectorAll ? root : document;
		try {
			scope.querySelectorAll('a[target], area[target], form[target]').forEach(function(el) {
				el.removeAttribute('target');
			});
		} catch (e) {}
	}

	function navigateSameTab(url) {
		if (typeof url !== 'string' || !url || url === 'about:blank') return;
		try {
			window.location.assign(url);
		} catch (e) {}
	}

	var originalOpen = typeof window.open === 'function' ? window.open.bind(window) : null;
	window.open = function(url) {
		navigateSameTab(url);
		return window;
	};
	window.__ghOriginalWindowOpen = originalOpen;

	document.addEventListener('click', function(event) {
		var el = event.target && event.target.closest ? event.target.closest('a[href], area[href]') : null;
		if (!el) {
			return;
		}
		var target = '';
		try {
			target = String(el.getAttribute('target') || '').trim().toLowerCase();
		} catch (e) {}
		try { el.removeAttribute('target'); } catch (e) {}
		if (!target || target === '_self') {
			return;
		}
		var href = '';
		try {
			href = String(el.href || el.getAttribute('href') || '').trim();
		} catch (e) {}
		if (!href || href.charAt(0) === '#' || href.toLowerCase().indexOf('javascript:') === 0) {
			return;
		}
		try { event.preventDefault(); } catch (e) {}
		try { event.stopImmediatePropagation(); } catch (e) {}
		try { event.stopPropagation(); } catch (e) {}
		navigateSameTab(href);
	}, true);

	document.addEventListener('submit', function(event) {
		var form = event.target;
		if (!form || !form.removeAttribute) {
			return;
		}
		var target = '';
		try {
			target = String(form.getAttribute('target') || '').trim().toLowerCase();
		} catch (e) {}
		try { form.removeAttribute('target'); } catch (e) {}
		if (target && target !== '_self') {
			try { event.preventDefault(); } catch (e) {}
			try { event.stopImmediatePropagation(); } catch (e) {}
			try { event.stopPropagation(); } catch (e) {}
			setTimeout(function() {
				try {
					if (typeof form.requestSubmit === 'function') {
						form.requestSubmit();
						return;
					}
				} catch (e) {}
				try {
					HTMLFormElement.prototype.submit.call(form);
				} catch (e) {}
			}, 0);
		}
	}, true);

	var observer = new MutationObserver(function(records) {
		for (var i = 0; i < records.length; i++) {
			var record = records[i];
			if (record.type === 'attributes' && record.attributeName === 'target' && record.target && record.target.removeAttribute) {
				try { record.target.removeAttribute('target'); } catch (e) {}
			}
			if (!record.addedNodes) continue;
			for (var j = 0; j < record.addedNodes.length; j++) {
				var node = record.addedNodes[j];
				if (node && node.querySelectorAll) stripTargets(node);
			}
		}
	});

	if (document.documentElement) {
		observer.observe(document.documentElement, {
			subtree: true,
			childList: true,
			attributes: true,
			attributeFilter: ['target'],
		});
	}

	stripTargets(document);
})();"""

_FINAL_SUBMIT_GUARD_JS = r"""(() => {
	if (window.__ghFinalSubmitGuardInstalled) return;
	window.__ghFinalSubmitGuardInstalled = true;
	window.__ghFinalSubmitGuardState = window.__ghFinalSubmitGuardState || { blocked: false, label: "", text: "", count: 0 };

	function normalize(text) {
		return String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
	}

	function getText(el) {
		if (!el) return '';
		return [
			el.getAttribute && el.getAttribute('aria-label'),
			el.getAttribute && el.getAttribute('title'),
			el.value,
			el.innerText,
			el.textContent,
		].filter(Boolean).join(' ');
	}

	function isVisible(el) {
		if (!el || !(el instanceof Element)) return false;
		const style = window.getComputedStyle(el);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	}

	function hasPasswordField(form) {
		try {
			return !!(form && form.querySelector && form.querySelector('input[type="password"]'));
		} catch (e) {
			return false;
		}
	}

	function recordBlock(el) {
		const text = normalize(getText(el));
		window.__ghFinalSubmitGuardState = {
			blocked: true,
			label: text || 'submit',
			text: getText(el) || '',
			count: Number((window.__ghFinalSubmitGuardState && window.__ghFinalSubmitGuardState.count) || 0) + 1,
		};
	}

	function looksLikeFinalSubmit(el) {
		const control = el && el.closest ? el.closest('button, input[type="submit"], input[type="button"], [role="button"]') : null;
		if (!control || !isVisible(control)) return false;
		if (control.disabled) return false;
		if (String(control.getAttribute && control.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;

		const form = control.form || (control.closest ? control.closest('form') : null);
		if (hasPasswordField(form)) return false;

		const text = normalize(getText(control));
		if (!text) return false;
		if (
			text.includes('sign in') ||
			text.includes('log in') ||
			text.includes('login') ||
			text.includes('create account') ||
			text.includes('register') ||
			text.includes('save and continue') ||
			text === 'next' ||
			text === 'continue' ||
			text.includes('continue to review') ||
			text.includes('apply with resume') ||
			text === 'apply'
		) {
			return false;
		}

		return (
			text === 'submit' ||
			text.includes('submit application') ||
			text.includes('review and submit') ||
			text.includes('finish and submit') ||
			text.includes('complete application') ||
			text.includes('confirm and submit') ||
			text.includes('send application')
		);
	}

	function shouldBlockFormSubmit(form, candidate) {
		if (hasPasswordField(form)) return false;
		if (candidate && looksLikeFinalSubmit(candidate)) return candidate.closest('button, input[type="submit"], input[type="button"], [role="button"]');
		try {
			const controls = Array.from(form.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"]'));
			return controls.find(looksLikeFinalSubmit) || null;
		} catch (e) {
			return null;
		}
	}

	document.addEventListener('click', function(event) {
		const control = event.target && event.target.closest ? event.target.closest('button, input[type="submit"], input[type="button"], [role="button"]') : null;
		if (!control || !looksLikeFinalSubmit(control)) return;
		recordBlock(control);
		try { event.preventDefault(); } catch (e) {}
		try { event.stopImmediatePropagation(); } catch (e) {}
		try { event.stopPropagation(); } catch (e) {}
	}, true);

	document.addEventListener('keydown', function(event) {
		const active = document.activeElement;
		if (!active || !looksLikeFinalSubmit(active)) return;
		if (event.key === 'Enter' || event.key === ' ') {
			recordBlock(active);
			try { event.preventDefault(); } catch (e) {}
			try { event.stopImmediatePropagation(); } catch (e) {}
			try { event.stopPropagation(); } catch (e) {}
		}
	}, true);

	document.addEventListener('submit', function(event) {
		const form = event.target;
		const submitter = event.submitter || document.activeElement;
		const blockedControl = shouldBlockFormSubmit(form, submitter);
		if (!blockedControl) return;
		recordBlock(blockedControl);
		try { event.preventDefault(); } catch (e) {}
		try { event.stopImmediatePropagation(); } catch (e) {}
		try { event.stopPropagation(); } catch (e) {}
	}, true);

	const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
	HTMLFormElement.prototype.requestSubmit = function(submitter) {
		const blockedControl = shouldBlockFormSubmit(this, submitter || document.activeElement);
		if (blockedControl) {
			recordBlock(blockedControl);
			return;
		}
		return nativeRequestSubmit.call(this, submitter);
	};

	const nativeSubmit = HTMLFormElement.prototype.submit;
	HTMLFormElement.prototype.submit = function() {
		const blockedControl = shouldBlockFormSubmit(this, document.activeElement);
		if (blockedControl) {
			recordBlock(blockedControl);
			return;
		}
		return nativeSubmit.call(this);
	};
})();"""

_READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS = r"""(() => {
	const state = window.__ghFinalSubmitGuardState || null;
	if (!state || !state.blocked) return null;
	window.__ghFinalSubmitGuardState = {
		blocked: false,
		label: '',
		text: '',
		count: Number(state.count || 0),
	};
	return state;
})();"""


async def install_same_tab_guard(agent: "Agent") -> None:
	"""Prevent sites from opening new tabs during job-application flows.

	This is best-effort:
	- installs a CDP init script once per browser session so future documents inherit it
	- reapplies the script on the current page every step to catch already-loaded DOM
	"""
	try:
		browser_session = getattr(agent, "browser_session", None)
		if browser_session is None:
			return

		session_key = id(browser_session)
		if session_key not in _SAME_TAB_GUARD_INSTALLED:
			try:
				await browser_session._cdp_add_init_script(_SAME_TAB_GUARD_JS)
				_SAME_TAB_GUARD_INSTALLED.add(session_key)
			except Exception as exc:
				logger.debug("step.same_tab_guard_init_failed", extra={"error": str(exc)})

		pages = []
		try:
			pages = list(await browser_session.get_pages())
		except Exception:
			pages = []
		if not pages:
			page = await browser_session.get_current_page()
			if page:
				pages = [page]
		for page in pages:
			await page.evaluate(_SAME_TAB_GUARD_JS)
	except Exception as exc:
		logger.debug("step.same_tab_guard_apply_failed", extra={"error": str(exc)})


async def install_final_submit_guard(agent: "Agent", *, allow_submit: bool) -> None:
	"""Install a runtime guard that blocks final application submission by default."""
	if allow_submit:
		return
	try:
		browser_session = getattr(agent, "browser_session", None)
		if browser_session is None:
			return

		session_key = id(browser_session)
		if session_key not in _FINAL_SUBMIT_GUARD_INSTALLED:
			try:
				await browser_session._cdp_add_init_script(_FINAL_SUBMIT_GUARD_JS)
				_FINAL_SUBMIT_GUARD_INSTALLED.add(session_key)
			except Exception as exc:
				logger.debug("step.final_submit_guard_init_failed", extra={"error": str(exc)})

		pages = []
		try:
			pages = list(await browser_session.get_pages())
		except Exception:
			pages = []
		if not pages:
			page = await browser_session.get_current_page()
			if page:
				pages = [page]
		for page in pages:
			await page.evaluate(_FINAL_SUBMIT_GUARD_JS)
	except Exception as exc:
		logger.debug("step.final_submit_guard_apply_failed", extra={"error": str(exc)})


async def consume_blocked_final_submit(agent: "Agent") -> dict[str, Any] | None:
	"""Return and clear the latest blocked final-submit attempt, if any."""
	try:
		browser_session = getattr(agent, "browser_session", None)
		if browser_session is None:
			return None
		page = await browser_session.get_current_page()
		if page is None:
			return None
		state = await page.evaluate(_READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS)
		return state if isinstance(state, dict) else None
	except Exception:
		return None

PHASE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"upload.*resume|resume.*upload|attach.*resume", re.IGNORECASE), "Uploading resume"),
    (re.compile(r"fill.*experience|experience", re.IGNORECASE), "Filling work experience"),
    (
        re.compile(r"personal.*info|personal.*information|contact.*info|contact.*information", re.IGNORECASE),
        "Filling personal information",
    ),
    (
        re.compile(r"additional.*question|supplemental.*question|extra.*question", re.IGNORECASE),
        "Answering additional questions",
    ),
    (re.compile(r"review|submit", re.IGNORECASE), "Preparing to submit"),
    (re.compile(r"navigate|open.*application|go to .*application", re.IGNORECASE), "Navigating to application"),
)


def infer_phase_from_goal(goal: str | None) -> str | None:
    """Map a raw agent goal to a user-friendly progress phase."""
    if not goal:
        return None

    normalized_goal = goal.strip()
    if not normalized_goal:
        return None

    for pattern, phase in PHASE_PATTERNS:
        if pattern.search(normalized_goal):
            return phase
    return None


def _summarize_actions(agent: "Agent") -> list[dict[str, Any]]:
    """Return the latest planned actions in a compact trace-friendly format."""
    last_output = agent.state.last_model_output
    if last_output is None:
        return []
    actions: list[dict[str, Any]] = []
    for action in last_output.action:
        action_data = action.model_dump(exclude_unset=True) if hasattr(action, "model_dump") else {}
        if not isinstance(action_data, dict) or not action_data:
            continue
        action_name = next(iter(action_data.keys()), "unknown")
        params = action_data.get(action_name, {})
        actions.append(
            {
                "action": action_name,
                "params": params if isinstance(params, dict) else {},
            }
        )
    return actions


def _summarize_results(agent: "Agent") -> list[dict[str, Any]]:
    """Return the latest action results in a compact trace-friendly format."""
    if not agent.history.history:
        return []
    last_entry = agent.history.history[-1]
    if not last_entry.result:
        return []
    summaries: list[dict[str, Any]] = []
    for result in last_entry.result:
        metadata = result.metadata or {}
        summaries.append(
            {
                "error": result.error,
                "extracted_content": result.extracted_content,
                "is_done": result.is_done,
                "success": result.success,
                "metadata": metadata,
            }
        )
    return summaries


class StepHooks:
    """Factory that produces bound ``on_step_start`` / ``on_step_end`` hooks.

    Parameters
    ----------
    job_id:
        Identifier for the current job (for logging / metrics).
    max_budget:
        Maximum LLM spend in USD for this job.  When exceeded the agent's
        ``state.stopped`` flag is set so it terminates at the next step.
    on_status_update:
        Optional async callback invoked after every step with a status dict
        that the worker can relay to VALET via callback.  Signature::

            async def on_status_update(status: dict) -> None
    """

    def __init__(
        self,
        job_id: str = "",
        max_budget: float = 0.50,
        on_status_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.job_id = job_id
        self.max_budget = max_budget
        self.on_status_update = on_status_update
        self._cumulative_cost: float = 0.0
        self._browser_use_cost: float = 0.0
        self._browser_use_prompt_tokens: int = 0
        self._browser_use_completion_tokens: int = 0
        self._domhand_cost_total: float = 0.0
        self._domhand_input_tokens_total: int = 0
        self._domhand_output_tokens_total: int = 0
        self._domhand_llm_calls_total: int = 0
        self._domhand_models: set[str] = set()
        self._stagehand_sources: set[str] = set()
        self._stagehand_calls: int = 0
        self._last_phase: str | None = None

    # ------------------------------------------------------------------
    # on_step_start
    # ------------------------------------------------------------------

    async def on_step_start(self, agent: Agent) -> None:
        """Called before each agent step.

        Responsibilities:
        - Check if the agent has been externally paused/stopped (HITL).
        - Log step start for observability.
        """
        step = agent.state.n_steps
        logger.debug(
            "step.start",
            extra={"job_id": self.job_id, "step": step},
        )

        browser_session = getattr(agent, "browser_session", None)
        attach_step_trace_context(browser_session, job_id=self.job_id)
        await install_same_tab_guard(agent)
        await maybe_tighten_max_actions_for_oracle_focus(agent)

        current_url = ""
        if browser_session is not None:
            try:
                page = await browser_session.get_current_page()
                if page is not None:
                    current_url = await page.get_url()
            except Exception:
                current_url = ""
        last_state = getattr(browser_session, "_gh_last_application_state", None)
        trace_payload = {
            "step": step,
            "page_url": current_url,
            "current_section": (last_state or {}).get("current_section", "") if isinstance(last_state, dict) else "",
            "page_context_key": (last_state or {}).get("page_context_key", "") if isinstance(last_state, dict) else "",
            "blocking_field_ids": (last_state or {}).get("blocking_field_ids", []) if isinstance(last_state, dict) else [],
            "blocking_field_keys": (last_state or {}).get("blocking_field_keys", []) if isinstance(last_state, dict) else [],
            "blocking_field_reasons": (last_state or {}).get("blocking_field_reasons", {}) if isinstance(last_state, dict) else {},
        }
        await publish_browser_session_trace(browser_session, "step_start", trace_payload)

        # If an external signal set the stopped flag, the agent loop will
        # honour it on the next iteration.  We don't need to do anything
        # extra here; just ensure it's visible in logs.
        if agent.state.stopped:
            logger.info(
                "step.start.agent_stopped",
                extra={"job_id": self.job_id, "step": step},
            )

    # ------------------------------------------------------------------
    # on_step_end
    # ------------------------------------------------------------------

    async def on_step_end(self, agent: Agent) -> None:
        """Called after each agent step.

        Responsibilities:
        - Track cumulative LLM cost and stop if budget exceeded.
        - Detect blocker keywords in the agent's last output.
        - Fire an optional status-update callback for the worker.
        """
        step = agent.state.n_steps
        last_output = agent.state.last_model_output
        browser_session = getattr(agent, "browser_session", None)
        attach_step_trace_context(browser_session, job_id=self.job_id)

        # ── Cost tracking ──────────────────────────────────────────
        usage = agent.history.usage
        browser_use_cost = 0.0
        usage_input_tokens = 0
        usage_output_tokens = 0
        if usage is not None:
            browser_use_cost = usage.total_cost or 0.0
            usage_input_tokens = usage.total_prompt_tokens or 0
            usage_output_tokens = usage.total_completion_tokens or 0
        self._browser_use_cost = browser_use_cost
        self._browser_use_prompt_tokens = usage_input_tokens
        self._browser_use_completion_tokens = usage_output_tokens

        last_entry = agent.history.history[-1] if agent.history.history else None
        step_cost: float | None = None
        step_input_tokens: int | None = None
        step_output_tokens: int | None = None
        step_model: str | None = None
        step_llm_calls: int | None = None
        if last_entry and last_entry.result:
            step_cost_total = 0.0
            step_input_total = 0
            step_output_total = 0
            step_llm_call_total = 0
            has_step_metadata = False
            has_input_tokens = False
            has_output_tokens = False
            has_llm_calls = False

            for result in last_entry.result:
                metadata = result.metadata or {}
                if 'step_cost' not in metadata:
                    continue
                has_step_metadata = True
                try:
                    step_cost_total += float(metadata.get('step_cost') or 0.0)
                except (TypeError, ValueError):
                    logger.debug('step.invalid_step_cost', extra={'job_id': self.job_id, 'step': step})
                if metadata.get('input_tokens') is not None:
                    has_input_tokens = True
                    step_input_total += int(metadata['input_tokens'])
                if metadata.get('output_tokens') is not None:
                    has_output_tokens = True
                    step_output_total += int(metadata['output_tokens'])
                if metadata.get('domhand_llm_calls') is not None:
                    has_llm_calls = True
                    step_llm_call_total += int(metadata['domhand_llm_calls'])
                if metadata.get('model'):
                    step_model = str(metadata['model'])

            if has_step_metadata:
                step_cost = step_cost_total
                self._domhand_cost_total += step_cost_total
                if has_input_tokens:
                    step_input_tokens = step_input_total
                    self._domhand_input_tokens_total += step_input_total
                if has_output_tokens:
                    step_output_tokens = step_output_total
                    self._domhand_output_tokens_total += step_output_total
                if has_llm_calls:
                    step_llm_calls = step_llm_call_total
                    self._domhand_llm_calls_total += step_llm_call_total
                if step_model:
                    self._domhand_models.add(step_model)

        # Planner cost from browser-use TokenCost (LiteLLM pricing) + DomHand tool LLM
        # from ActionResult.metadata step_cost (ghosthands.config.models.estimate_cost).
        # Totals are estimates; reconcile with provider/VALET billing separately.
        stagehand_usage = get_stagehand_usage(browser_session)
        self._stagehand_sources.update(stagehand_usage["sources"])
        self._stagehand_calls = int(stagehand_usage["calls"])
        cost_summary = self.cost_summary
        self._cumulative_cost = float(cost_summary["total_tracked_cost_usd"])

        if self._cumulative_cost >= self.max_budget:
            logger.warning(
                "step.budget_exceeded",
                extra={
                    "job_id": self.job_id,
                    "step": step,
                    "cost": self._cumulative_cost,
                    "budget": self.max_budget,
                },
            )
            agent.state.stopped = True

        # ── Blocker detection from done text ───────────────────────
        blocker_detected: str | None = None
        if agent.history.is_done() and last_entry:
            for result in last_entry.result:
                if result.extracted_content and "blocker:" in result.extracted_content.lower():
                    blocker_detected = result.extracted_content

        # ── Status update callback ─────────────────────────────────
        if self.on_status_update is not None:
            last_state = getattr(browser_session, "_gh_last_application_state", None)
            current_url = ""
            if browser_session is not None:
                try:
                    page = await browser_session.get_current_page()
                    if page is not None:
                        current_url = await page.get_url()
                except Exception:
                    current_url = ""
            status: dict[str, Any] = {
                "job_id": self.job_id,
                "step": step,
                "step_cost": round(step_cost, 6) if step_cost is not None else None,
                "cost_usd": round(self._cumulative_cost, 6),
                "cost_summary": cost_summary,
                "is_done": agent.history.is_done(),
                "page_url": current_url,
                "actions": _summarize_actions(agent),
                "results": _summarize_results(agent),
            }
            if usage is not None:
                status["usage_input_tokens"] = usage_input_tokens
                status["usage_output_tokens"] = usage_output_tokens
            if step_input_tokens is not None:
                status["input_tokens"] = step_input_tokens
            if step_output_tokens is not None:
                status["output_tokens"] = step_output_tokens
            if step_model:
                status["model"] = step_model
            if step_llm_calls is not None:
                status["domhand_llm_calls"] = step_llm_calls
            if blocker_detected:
                status["blocker"] = blocker_detected
            if last_output is not None:
                eval_text = last_output.current_state.evaluation_previous_goal
                if eval_text:
                    status["evaluation_previous_goal"] = eval_text
                memory = last_output.current_state.memory
                if memory:
                    status["memory"] = memory
                next_goal = last_output.next_goal
                if next_goal:
                    status["next_goal"] = next_goal
                phase = infer_phase_from_goal(next_goal)
                if phase is not None and phase != self._last_phase:
                    status["phase"] = phase
                    self._last_phase = phase
            if isinstance(last_state, dict):
                status["current_section"] = last_state.get("current_section", "")
                status["page_context_key"] = last_state.get("page_context_key", "")
                status["blocking_field_ids"] = last_state.get("blocking_field_ids", [])
                status["blocking_field_keys"] = last_state.get("blocking_field_keys", [])
                status["blocking_field_reasons"] = last_state.get("blocking_field_reasons", {})
                status["blocking_field_state_changes"] = last_state.get("blocking_field_state_changes", {})
                status["single_active_blocker"] = last_state.get("single_active_blocker")
            try:
                await self.on_status_update(status)
            except Exception:
                logger.exception(
                    "step.status_update_failed",
                    extra={"job_id": self.job_id, "step": step},
                )
            await publish_browser_session_trace(browser_session, "step_end", status)

        logger.debug(
            "step.end",
            extra={
                "job_id": self.job_id,
                "step": step,
                "cost_usd": round(self._cumulative_cost, 6),
                "done": agent.history.is_done(),
            },
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def cumulative_cost(self) -> float:
        """Return the cumulative LLM cost tracked so far."""
        return self._cumulative_cost

    @property
    def cost_summary(self) -> dict[str, Any]:
        """Return the unified tracked-cost summary for the current run."""
        return build_cost_summary(
            browser_use_cost_usd=self._browser_use_cost,
            browser_use_prompt_tokens=self._browser_use_prompt_tokens,
            browser_use_completion_tokens=self._browser_use_completion_tokens,
            domhand_cost_usd=self._domhand_cost_total,
            domhand_prompt_tokens=self._domhand_input_tokens_total,
            domhand_completion_tokens=self._domhand_output_tokens_total,
            domhand_llm_calls=self._domhand_llm_calls_total,
            domhand_models=sorted(self._domhand_models),
            stagehand_used=bool(self._stagehand_sources),
            stagehand_calls=self._stagehand_calls,
            stagehand_sources=sorted(self._stagehand_sources),
        ).to_dict()
