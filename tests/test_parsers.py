"""Tests for SectionParser."""

import pytest

from zeperion.models import GlobalStatus, TestStatus
from zeperion.parsers import SectionParser


class TestSectionParser:
    """Test SectionParser functionality."""

    def test_extract_field_basic(self):
        """Test basic field extraction."""
        text = """
TASK_ID: task-123
TEST_STATUS: PASS
"""
        parser = SectionParser(text)
        assert parser.extract_field("TASK_ID") == "task-123"
        assert parser.extract_field("TEST_STATUS") == "PASS"

    def test_extract_field_case_insensitive(self):
        """Test case-insensitive field matching."""
        text = """
task_id: task-456
Test_Status: FAIL
"""
        parser = SectionParser(text)
        assert parser.extract_field("TASK_ID") == "task-456"
        assert parser.extract_field("TEST_STATUS") == "FAIL"

    def test_extract_field_with_spaces(self):
        """Test field extraction with extra spaces."""
        text = """
TASK_ID   :   task-789
TEST_STATUS :PASS
"""
        parser = SectionParser(text)
        assert parser.extract_field("TASK_ID") == "task-789"
        assert parser.extract_field("TEST_STATUS") == "PASS"

    def test_extract_field_missing(self):
        """Test missing field returns None."""
        text = "TASK_ID: task-123"
        parser = SectionParser(text)
        assert parser.extract_field("MISSING_FIELD") is None

    def test_extract_enum_valid(self):
        """Test enum extraction with valid value."""
        text = "TEST_STATUS: PASS"
        parser = SectionParser(text)
        result = parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.PENDING)
        assert result == TestStatus.PASS

    def test_extract_enum_case_insensitive(self):
        """Test enum extraction with case variation."""
        text = "TEST_STATUS: pass"
        parser = SectionParser(text)
        result = parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.PENDING)
        assert result == TestStatus.PASS

    def test_extract_enum_invalid_uses_default(self):
        """Test enum extraction with invalid value uses default."""
        text = "TEST_STATUS: INVALID"
        parser = SectionParser(text)
        result = parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.PENDING)
        assert result == TestStatus.PENDING

    def test_extract_enum_missing_uses_default(self):
        """Test enum extraction with missing field uses default."""
        text = "OTHER_FIELD: value"
        parser = SectionParser(text)
        result = parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.PENDING)
        assert result == TestStatus.PENDING

    def test_extract_section_basic(self):
        """Test basic section extraction."""
        text = """
LESSONS:
- Lesson 1
- Lesson 2
- Lesson 3

NEXT_SECTION:
content
"""
        parser = SectionParser(text)
        result = parser.extract_section("LESSONS")
        assert "Lesson 1" in result
        assert "Lesson 2" in result
        assert "Lesson 3" in result
        assert "NEXT_SECTION" not in result

    def test_extract_section_case_insensitive(self):
        """Test section extraction is case-insensitive."""
        text = """
lessons:
- Lesson 1
"""
        parser = SectionParser(text)
        result = parser.extract_section("LESSONS")
        assert "Lesson 1" in result

    def test_extract_section_empty(self):
        """Test empty section returns empty string."""
        text = """
LESSONS:

NEXT_SECTION:
"""
        parser = SectionParser(text)
        result = parser.extract_section("LESSONS")
        assert result == ""

    def test_extract_section_missing(self):
        """Test missing section returns empty string."""
        text = "OTHER_SECTION: content"
        parser = SectionParser(text)
        result = parser.extract_section("LESSONS")
        assert result == ""

    def test_extract_list_with_bullets(self):
        """Test list extraction with bullet points."""
        text = """
LESSONS:
- Lesson 1
- Lesson 2
* Lesson 3
• Lesson 4
"""
        parser = SectionParser(text)
        result = parser.extract_list("LESSONS")
        assert result == ["Lesson 1", "Lesson 2", "Lesson 3", "Lesson 4"]

    def test_extract_list_with_numbers(self):
        """Test list extraction with numbered items."""
        text = """
LESSONS:
1. Lesson 1
2. Lesson 2
3. Lesson 3
"""
        parser = SectionParser(text)
        result = parser.extract_list("LESSONS")
        assert result == ["Lesson 1", "Lesson 2", "Lesson 3"]

    def test_extract_list_plain_lines(self):
        """Test list extraction with plain lines."""
        text = """
LESSONS:
Lesson 1
Lesson 2
Lesson 3
"""
        parser = SectionParser(text)
        result = parser.extract_list("LESSONS", strip_bullets=False)
        assert result == ["Lesson 1", "Lesson 2", "Lesson 3"]

    def test_extract_list_empty(self):
        """Test empty list returns empty array."""
        text = """
LESSONS:

NEXT_SECTION:
"""
        parser = SectionParser(text)
        result = parser.extract_list("LESSONS")
        assert result == []

    def test_has_field(self):
        """Test field existence check."""
        text = "TASK_ID: task-123"
        parser = SectionParser(text)
        assert parser.has_field("TASK_ID") is True
        assert parser.has_field("MISSING") is False

    def test_has_section(self):
        """Test section existence check."""
        text = """
LESSONS:
- Lesson 1
"""
        parser = SectionParser(text)
        assert parser.has_section("LESSONS") is True
        assert parser.has_section("MISSING") is False

    def test_max_section_lines_limit(self):
        """Test section line limit."""
        lines = "\n".join([f"Line {i}" for i in range(2000)])
        text = f"SECTION:\n{lines}"
        parser = SectionParser(text, max_section_lines=100)
        result = parser.extract_section("SECTION")
        assert result.count("\n") < 100

    def test_real_world_agent_output(self):
        """Test parsing real-world agent output."""
        text = """
I've analyzed the requirement and created a plan.

TASK_ID: implement-user-auth
GLOBAL_STATUS: CONTINUE

TASK DESCRIPTION:
Implement user authentication with JWT tokens.

LESSONS:
- Always validate JWT signatures
- Use secure password hashing (bcrypt)
- Implement rate limiting for login attempts

Next steps will be implementation.
"""
        parser = SectionParser(text)

        assert parser.extract_field("TASK_ID") == "implement-user-auth"
        assert parser.extract_enum("GLOBAL_STATUS", GlobalStatus, GlobalStatus.CONTINUE) == GlobalStatus.CONTINUE

        lessons = parser.extract_list("LESSONS")
        assert len(lessons) == 3
        assert "JWT signatures" in lessons[0]
        assert "bcrypt" in lessons[1]
        assert "rate limiting" in lessons[2]


class TestEnumDecorationTolerance:
    """Real LLM output sometimes wraps enum values in markdown / quotes.

    These cases come straight from observed Claude outputs (see commit notes).
    """

    @pytest.mark.parametrize(
        "raw_value,expected",
        [
            ("PASS", TestStatus.PASS),
            ("**PASS**", TestStatus.PASS),
            ("*PASS*", TestStatus.PASS),
            ("__PASS__", TestStatus.PASS),
            ("`PASS`", TestStatus.PASS),
            ("```PASS```", TestStatus.PASS),
            ('"PASS"', TestStatus.PASS),
            ("'PASS'", TestStatus.PASS),
            ("**pass**", TestStatus.PASS),
            ("PASS.", TestStatus.PASS),
            ("**PASS**.", TestStatus.PASS),
            ("  **PASS**  ", TestStatus.PASS),
        ],
    )
    def test_decorated_test_status_resolves_to_enum(self, raw_value, expected):
        parser = SectionParser(f"TEST_STATUS: {raw_value}\n")
        assert parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.PENDING) == expected

    @pytest.mark.parametrize(
        "raw_value,expected",
        [
            ("DONE", GlobalStatus.DONE),
            ("**DONE**", GlobalStatus.DONE),
            ("`CONTINUE`", GlobalStatus.CONTINUE),
            ("**BLOCKED**", GlobalStatus.BLOCKED),
        ],
    )
    def test_decorated_global_status_resolves(self, raw_value, expected):
        parser = SectionParser(f"GLOBAL_STATUS: {raw_value}\n")
        assert parser.extract_enum("GLOBAL_STATUS", GlobalStatus, GlobalStatus.CONTINUE) == expected

    def test_unrecognised_value_still_falls_back_to_default(self):
        parser = SectionParser("TEST_STATUS: **NOT_A_REAL_STATUS**\n")
        assert (
            parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.PENDING)
            == TestStatus.PENDING
        )
