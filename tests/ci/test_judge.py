"""Tests for judge functionality."""

import json

import pytest

from browser_use.agent.judge import construct_judge_messages
from browser_use.agent.views import JudgementResult


def test_judgement_result_schema():
	"""Test that JudgementResult has all required fields."""
	# Test with all fields
	result = JudgementResult(
		reasoning='Task completed successfully',
		verdict=True,
		failure_reason='',
		impossible_task=False,
		reached_captcha=False,
	)

	assert result.reasoning == 'Task completed successfully'
	assert result.verdict is True
	assert result.failure_reason == ''
	assert result.impossible_task is False
	assert result.reached_captcha is False

	# Test with defaults
	result_defaults = JudgementResult(
		verdict=False,
		failure_reason='Task failed due to missing login credentials',
	)

	assert result_defaults.verdict is False
	assert result_defaults.failure_reason == 'Task failed due to missing login credentials'
	assert result_defaults.impossible_task is False  # Default
	assert result_defaults.reached_captcha is False  # Default

	# Test serialization includes new fields
	data = result.model_dump()
	assert 'impossible_task' in data
	assert 'reached_captcha' in data


def test_judgement_result_impossible_task():
	"""Test impossible_task field scenarios."""
	# Impossible task due to missing credentials
	result_impossible = JudgementResult(
		reasoning='Task requires login but no credentials were provided',
		verdict=False,
		failure_reason='Missing login credentials',
		impossible_task=True,
		reached_captcha=False,
	)

	assert result_impossible.impossible_task is True
	assert result_impossible.verdict is False

	# Achievable task that just failed
	result_achievable = JudgementResult(
		reasoning='Agent made poor navigation choices',
		verdict=False,
		failure_reason='Navigation error',
		impossible_task=False,
		reached_captcha=False,
	)

	assert result_achievable.impossible_task is False


def test_judgement_result_reached_captcha():
	"""Test reached_captcha field scenarios."""
	# Task blocked by captcha
	result_captcha = JudgementResult(
		reasoning='Agent was blocked by reCAPTCHA on the login page',
		verdict=False,
		failure_reason='Blocked by captcha',
		impossible_task=True,
		reached_captcha=True,
	)

	assert result_captcha.reached_captcha is True
	assert result_captcha.verdict is False

	# Task without captcha
	result_no_captcha = JudgementResult(
		reasoning='Task completed without any anti-bot measures',
		verdict=True,
		failure_reason='',
		impossible_task=False,
		reached_captcha=False,
	)

	assert result_no_captcha.reached_captcha is False


def test_judge_prompt_includes_new_fields():
	"""Test that the judge system prompt includes instructions for new fields."""
	messages = construct_judge_messages(
		task='Test task',
		final_result='Test result',
		agent_steps=['Step 1: Navigate', 'Step 2: Click'],
		screenshot_paths=[],
		max_images=10,
	)

	# Get system prompt
	system_prompt = messages[0].content

	# Check that the prompt mentions the new fields
	assert 'impossible_task' in system_prompt
	assert 'reached_captcha' in system_prompt
	assert 'IMPOSSIBLE TASK DETECTION' in system_prompt
	assert 'CAPTCHA DETECTION' in system_prompt

	# Check that response format includes the new fields
	assert '"impossible_task": true or false' in system_prompt
	assert '"reached_captcha": true or false' in system_prompt
