"""Test configuration."""

import pytest


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
