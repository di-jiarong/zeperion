"""Test configuration."""

import re

import pytest

# Rich / Typer help text renders option names across multiple ANSI spans
# (e.g. ``--config`` is emitted as ``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-config\\x1b[0m``),
# which defeats naive substring assertions.  Strip ANSI before comparing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return _ANSI_RE.sub("", text)


@pytest.fixture
def sample_agent_output():
    """Sample agent output for testing."""
    return """
I've analyzed the requirement and created a plan.

TASK_ID: implement-user-auth
GLOBAL_STATUS: CONTINUE
TEST_STATUS: PENDING

TASK DESCRIPTION:
Implement user authentication with JWT tokens.

LESSONS:
- Always validate JWT signatures
- Use secure password hashing (bcrypt)
- Implement rate limiting for login attempts

Next steps will be implementation.
"""


@pytest.fixture
def sample_tester_output():
    """Sample tester output for testing."""
    return """
I've run the tests and here are the results.

TEST_STATUS: PASS
GLOBAL_STATUS: DONE

All tests passed successfully:
- test_user_login: PASS
- test_user_logout: PASS
- test_invalid_credentials: PASS

LESSONS:
- Edge cases are well covered
- Performance is acceptable
"""


@pytest.fixture
def sample_developer_output():
    """Sample developer output for testing."""
    return """
I've implemented the requested feature.

LESSONS:
- Used bcrypt for password hashing
- Implemented JWT token generation
- Added rate limiting middleware
"""
