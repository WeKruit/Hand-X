"""Fixtures for interaction tests."""

import pytest

from browser_use.tools.service import Tools


@pytest.fixture
def tools():
	"""Create and provide a Tools instance."""
	return Tools()
