"""Section parser for LLM output."""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class MissingRequiredFieldError(ValueError):
    """Raised when a *required* field is absent or unrecognisable.

    Used by :meth:`SectionParser.extract_required_enum` for fields whose
    absence must NOT silently default. The classic example is
    ``GLOBAL_STATUS`` for the Planner / Tester: the previous behaviour of
    "missing → CONTINUE" combined with ``max_rounds=50`` could quietly
    burn a whole batch of expensive LLM calls when a single response
    forgot the marker.

    Carries the field name and the offending value so callers can build
    a human-readable ``last_error``.
    """

    def __init__(self, field_name: str, value: str | None = None):
        self.field_name = field_name
        self.value = value
        if value is None:
            super().__init__(f"Required field {field_name!r} is missing")
        else:
            super().__init__(
                f"Required field {field_name!r} has unrecognised value {value!r}"
            )


# Characters an LLM commonly prepends to a marker line: indentation plus
# Markdown decoration. Real Claude/Opus output writes markers as headings
# (``## GLOBAL_STATUS: CONTINUE``) or bold (``**GLOBAL_STATUS:** CONTINUE``).
# Before this was tolerated, a heading-style ``GLOBAL_STATUS`` failed the
# ``^\s*`` anchor, the field read as "missing", and a passing review got
# force-collapsed to BLOCKED by extract_required_enum. Keep this in sync
# across extract_field / extract_section / has_section and the
# next-section boundary in extract_section. ``-`` is last so it is literal.
_LINE_PREFIX = r"[ \t#>*_`-]*"

# Trailing part of a section *label* line after its colon: optional spaces,
# then optional mirror markers (so ``**PLAN:**`` and ``## PLAN:`` both close
# cleanly), then optional spaces to end of line.
_SECTION_LABEL_TAIL = r"[ \t]*[*_`#>-]*[ \t]*"


class SectionParser:
    """
    Lenient parser for structured LLM output.

    Handles common formatting variations:
    - Case insensitivity
    - Extra whitespace
    - Leading Markdown decoration (``#`` headings, ``**bold**``, blockquotes)
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

    def extract_field(self, field_name: str) -> str | None:
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
        # Two line shapes, tried most-specific first:
        #   1. Decorated *label*: ``**GLOBAL_STATUS:** CONTINUE`` or
        #      ``## TASK_ID: t1``. The markers belong to the label, so the
        #      mirrored run right after the colon is consumed and kept out
        #      of the value.
        #   2. Plain label: ``TEST_STATUS: **PASS**``. No leading markers,
        #      so everything after the colon (bold value included) is the
        #      value, preserved verbatim for downstream decoration
        #      stripping in enum/pr-title handling.
        decorated = (
            rf"(?i)^[ \t]*[#>*_`-]+[ \t]*{normalized}\s*:"
            rf"[ \t]*[*_`#>-]*[ \t]*(.+?)\s*$"
        )
        plain = rf"(?i)^[ \t]*{normalized}\s*:\s*(.+?)\s*$"

        for pattern in (decorated, plain):
            match = re.search(pattern, self.text, re.MULTILINE)
            if match:
                value = match.group(1).strip()
                if value:
                    return value
        return None

    def extract_enum(
        self,
        field_name: str,
        enum_class: type[Any],
        default: Any,
    ) -> Any:
        """Extract an enum field, tolerating common LLM-introduced noise.

        We have observed real Claude responses emit values like
        ``**PASS**`` (markdown bold), ``"PASS"`` (quoted),
        ``` `PASS` ``` (code-fenced), or trailing punctuation. All of these
        should still resolve to ``TestStatus.PASS``.

        Args:
            field_name: Field name.
            enum_class: Enum class.
            default: Default value when not found or unrecognised.
        """
        value = self.extract_field(field_name)
        if not value:
            return default

        resolved = _resolve_enum(value, enum_class)
        if resolved is not None:
            return resolved

        logger.warning(
            f"Invalid {field_name} value: '{value}', using default: {default}"
        )
        return default

    def extract_required_enum(
        self,
        field_name: str,
        enum_class: type[Any],
    ) -> Any:
        """Like :meth:`extract_enum`, but raises when the field is absent
        or the value cannot be resolved to a member of ``enum_class``.

        This exists specifically so the workflow can distinguish "the
        agent legitimately said CONTINUE" from "the agent forgot to
        emit GLOBAL_STATUS at all". Silently defaulting in the second
        case used to put the workflow into an infinite Planner→Dev→Tester
        loop until ``max_rounds`` ran out.

        Raises:
            MissingRequiredFieldError: When the field is absent, empty,
                or its value (after decoration stripping) does not match
                any enum member.
        """
        value = self.extract_field(field_name)
        if not value:
            raise MissingRequiredFieldError(field_name)

        resolved = _resolve_enum(value, enum_class)
        if resolved is not None:
            return resolved

        raise MissingRequiredFieldError(field_name, value)

    def extract_section(
        self,
        section_name: str,
        stop_markers: list[str] | None = None,
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
        # Find section start. ``_SECTION_LABEL_TAIL`` lets a decorated
        # label like ``**PLAN:**`` (trailing bold) or ``## PLAN:`` close
        # cleanly — the markers after the colon are part of the label.
        normalized = section_name.replace("_", r"[\s_]")
        pattern = rf"(?i)^{_LINE_PREFIX}{normalized}\s*:{_SECTION_LABEL_TAIL}$"
        match = re.search(pattern, self.text, re.MULTILINE)

        if not match:
            return ""

        start = match.end()

        # Find section end
        remaining = self.text[start:]

        # Default stop: next section marker (WORD:), tolerating a leading
        # Markdown prefix like ``## FIX_REQUEST:`` so headings still act as
        # section boundaries instead of being swallowed.
        next_section = re.search(rf"\n{_LINE_PREFIX}[A-Z][A-Z_\s]*:", remaining)
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
        pattern = rf"(?i)^{_LINE_PREFIX}{normalized}\s*:{_SECTION_LABEL_TAIL}$"
        return re.search(pattern, self.text, re.MULTILINE) is not None


_DECORATION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\*\*(.+?)\*\*$"),   # **bold**
    re.compile(r"^\*(.+?)\*$"),         # *italic*
    re.compile(r"^__(.+?)__$"),         # __bold__
    re.compile(r"^_(.+?)_$"),           # _italic_
    re.compile(r"^`+(.+?)`+$"),         # `code` / ```code```
    re.compile(r'^"(.+?)"$'),           # "quoted"
    re.compile(r"^'(.+?)'$"),           # 'quoted'
)


# Leading run of whitespace + Markdown markers that a bold/heading field
# name can leave glued to the *value*: ``**GLOBAL_STATUS:** CONTINUE`` is
# captured as ``** CONTINUE``. Stripping this lead (for enum resolution
# only) recovers ``CONTINUE``.
_LEADING_MARKER_RUN = re.compile(r"^[\s*_`#>-]+")


def _resolve_enum(value: str, enum_class: type[Any]) -> Any | None:
    """Best-effort resolve ``value`` to a member of ``enum_class``.

    Tries, in order: the raw value, the decoration-stripped value, and the
    value with a leading Markdown-marker run removed (covers bold/heading
    field names like ``**GLOBAL_STATUS:** CONTINUE`` whose value captures
    as ``** CONTINUE``). Returns the member or ``None`` if nothing matches.
    """
    candidates: list[str] = [value]

    stripped = _strip_decorations(value)
    if stripped and stripped not in candidates:
        candidates.append(stripped)

    lead = _LEADING_MARKER_RUN.sub("", value).strip()
    if lead and lead not in candidates:
        candidates.append(lead)
        lead_stripped = _strip_decorations(lead)
        if lead_stripped and lead_stripped not in candidates:
            candidates.append(lead_stripped)

    for cand in candidates:
        try:
            return enum_class(cand)
        except (ValueError, KeyError):
            pass

    for cand in candidates:
        cand_upper = cand.upper()
        for member in enum_class:
            if member.value.upper() == cand_upper:
                return member
    return None


def _strip_decorations(value: str) -> str:
    """Iteratively peel markdown/quote wrappers off ``value``.

    Returns the inner content with surrounding punctuation cleaned up,
    or the original value if nothing matched.
    """
    cleaned = value.strip()
    # Drop trailing punctuation that LLMs occasionally append.
    cleaned = cleaned.rstrip(".,;:!?")
    # Repeatedly peel decorations until stable; bail after a few rounds to
    # avoid pathological inputs.
    for _ in range(5):
        previous = cleaned
        for pattern in _DECORATION_PATTERNS:
            m = pattern.match(cleaned)
            if m:
                cleaned = m.group(1).strip()
                break
        if cleaned == previous:
            break
    return cleaned


def parse_agent_output(
    text: str,
    expected_fields: dict[str, Any] | None = None,
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
