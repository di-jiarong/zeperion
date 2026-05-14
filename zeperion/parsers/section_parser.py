"""Section parser for LLM output."""

import logging
import re
from typing import Any, Optional, Type

logger = logging.getLogger(__name__)


class SectionParser:
    """
    Lenient parser for structured LLM output.

    Handles common formatting variations:
    - Case insensitivity
    - Extra whitespace
    - Missing sections
    - Malformed markers
    """

    def __init__(self, text: str, max_section_lines: int = 1000):
        """
        Initialize parser.

        Args:
            text: Text to parse
            max_section_lines: Maximum lines to extract per section
        """
        self.text = text
        self.max_section_lines = max_section_lines

    def extract_field(self, field_name: str) -> Optional[str]:
        """
        Extract a single-line field value.

        Matches patterns like:
        - FIELD_NAME: value
        - FIELD_NAME : value
        - field_name: value
        - Field Name: value

        Args:
            field_name: Field name to extract

        Returns:
            Extracted value or None
        """
        # Normalize field name for matching (allow spaces and underscores)
        normalized = field_name.replace("_", r"[\s_]")
        pattern = rf"(?i)^\s*{normalized}\s*:\s*(.+?)\s*$"

        match = re.search(pattern, self.text, re.MULTILINE)
        if match:
            value = match.group(1).strip()
            return value if value else None
        return None

    def extract_enum(
        self,
        field_name: str,
        enum_class: Type[Any],
        default: Any,
    ) -> Any:
        """
        Extract an enum field value with fallback.

        Args:
            field_name: Field name
            enum_class: Enum class
            default: Default value if not found or invalid

        Returns:
            Enum value or default
        """
        value = self.extract_field(field_name)
        if not value:
            return default

        # Try exact match
        try:
            return enum_class(value)
        except (ValueError, KeyError):
            pass

        # Try case-insensitive match
        value_upper = value.upper()
        for member in enum_class:
            if member.value.upper() == value_upper:
                return member

        logger.warning(
            f"Invalid {field_name} value: '{value}', using default: {default}"
        )
        return default

    def extract_section(
        self,
        section_name: str,
        stop_markers: Optional[list[str]] = None,
    ) -> str:
        """
        Extract multi-line section content.

        Matches:
        SECTION_NAME:
        content line 1
        content line 2

        Stops at:
        - Next section marker (uppercase word + colon)
        - Explicit stop marker
        - End of text
        - Max lines limit

        Args:
            section_name: Section name to extract
            stop_markers: Optional list of patterns that end the section

        Returns:
            Section content (stripped)
        """
        # Find section start
        normalized = section_name.replace("_", r"[\s_]")
        pattern = rf"(?i)^\s*{normalized}\s*:\s*$"
        match = re.search(pattern, self.text, re.MULTILINE)

        if not match:
            return ""

        start = match.end()

        # Find section end
        remaining = self.text[start:]

        # Default stop: next section marker (WORD:)
        next_section = re.search(r"\n\s*[A-Z][A-Z_\s]*:", remaining)
        end = next_section.start() if next_section else len(remaining)

        # Check explicit stop markers
        if stop_markers:
            for marker in stop_markers:
                stop_match = re.search(marker, remaining)
                if stop_match and stop_match.start() < end:
                    end = stop_match.start()

        # Extract and limit lines
        content = remaining[:end]
        lines = content.split("\n")

        if len(lines) > self.max_section_lines:
            logger.warning(
                f"Section '{section_name}' exceeds {self.max_section_lines} lines, truncating"
            )
            lines = lines[:self.max_section_lines]

        return "\n".join(lines).strip()

    def extract_list(
        self,
        section_name: str,
        strip_bullets: bool = True,
    ) -> list[str]:
        """
        Extract a list from a section.

        Handles:
        - Bullet points (-, *, •)
        - Numbered lists (1., 2.)
        - Plain lines

        Args:
            section_name: Section name
            strip_bullets: Whether to remove bullet markers

        Returns:
            List of items
        """
        content = self.extract_section(section_name)
        if not content:
            return []

        items = []
        saw_blank_after_items = False
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                if items:
                    saw_blank_after_items = True
                continue

            # A blank line after list items usually means following prose is outside the list.
            if saw_blank_after_items:
                break

            if strip_bullets:
                # Remove common bullet markers
                line = re.sub(r"^[-*•]\s+", "", line)
                line = re.sub(r"^\d+\.\s+", "", line)

            if line:
                items.append(line)

        return items

    def has_field(self, field_name: str) -> bool:
        """
        Check if a field exists.

        Args:
            field_name: Field name to check

        Returns:
            True if field exists
        """
        return self.extract_field(field_name) is not None

    def has_section(self, section_name: str) -> bool:
        """
        Check if a section exists.

        Args:
            section_name: Section name to check

        Returns:
            True if section exists
        """
        normalized = section_name.replace("_", r"[\s_]")
        pattern = rf"(?i)^\s*{normalized}\s*:\s*$"
        return re.search(pattern, self.text, re.MULTILINE) is not None


def parse_agent_output(
    text: str,
    expected_fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Parse agent output with validation.

    Args:
        text: Raw agent output
        expected_fields: Optional dict of field_name -> (enum_class, default)

    Returns:
        Parsed fields dict

    Example:
        >>> parse_agent_output(text, {
        ...     "TASK_ID": (str, None),
        ...     "TEST_STATUS": (TestStatus, TestStatus.PENDING),
        ... })
    """
    parser = SectionParser(text)
    result = {}

    if expected_fields:
        for field_name, (field_type, default) in expected_fields.items():
            if hasattr(field_type, "__members__"):  # Enum
                result[field_name] = parser.extract_enum(
                    field_name, field_type, default
                )
            else:
                result[field_name] = parser.extract_field(field_name) or default

    return result
