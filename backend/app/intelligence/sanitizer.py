"""Sanitiser for untrusted customer free-text.

Direct and indirect prompt injection is the primary risk when customer-supplied
text reaches a model prompt, so everything crosses this boundary first. It is a
real, callable step with concrete rules rather than a prompt instruction or a
vague "sanitise" comment.

Three concrete rules, in order:

1. **Strip control characters.** Everything in Unicode categories Cc (control)
   and Cf (format) is removed. Cf matters as much as Cc -- it covers the
   bidirectional overrides (U+202A-U+202E, U+2066-U+2069) and zero-width joiners
   that are used to hide instruction text from a human reviewer while leaving it
   perfectly readable to a model. Tab/newline/carriage-return are folded to a
   single space first so words do not get glued together.

2. **Truncate to a hard 500-character cap.** Applied after stripping, so a
   payload cannot pad itself past the cap with invisible characters. This
   mitigates both JSON-breaking payloads and context flooding.

3. **Flag instruction-like patterns.** Matches are recorded by name, not
   removed -- the note still reaches the model as data, and the flag travels
   with the decision so the audit trail shows exactly what was detected. The
   one exception is a delimiter-escape attempt, which IS neutralised, because
   leaving it intact would let the note close the untrusted block and write
   into the instruction region of the prompt.

This module is deliberately dependency-free and deterministic: same input,
same report, every time.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

from app.intelligence.schemas import SanitizationReport

#: Hard cap on note length, applied after control characters are stripped.
MAX_NOTE_LENGTH = 500

#: The delimiter the prompt builder wraps untrusted content in. Defined here so
#: the sanitiser and the prompt builder cannot drift apart.
UNTRUSTED_BLOCK_TAG = "untrusted_customer_data"

#: Whitespace that is a control character but carries real meaning in a note.
#: Folded to a space rather than deleted.
_MEANINGFUL_WHITESPACE = {"\t", "\n", "\r", "\v", "\f"}

#: Named instruction-like patterns. Named, not anonymous, so a flagged note
#: says WHICH pattern fired in the audit trail and in the demo UI.
INJECTION_PATTERNS: List[Tuple[str, str]] = [
    (
        "ignore_previous_instructions",
        r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
        r"(?:instruction|prompt|rule|direction)",
    ),
    (
        "disregard_instructions",
        r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier|system)",
    ),
    ("system_prompt_reference", r"system\s+prompt|your\s+instructions\b"),
    (
        "role_reassignment",
        r"you\s+are\s+now\b|act\s+as\s+(?:a|an|the)\b|pretend\s+to\s+be\b",
    ),
    ("new_instructions", r"new\s+instruction|updated\s+instruction"),
    (
        "action_injection",
        r"\b(?:approve|authorize|authorise|issue|grant|process)\s+(?:the\s+|a\s+|my\s+)?"
        r"(?:refund|payment|discount|retry|charge)",
    ),
    ("override_directive", r"\boverride\b|\bbypass\b|\bignore\s+the\s+polic"),
    ("delimiter_escape_attempt", rf"</?\s*{UNTRUSTED_BLOCK_TAG}\s*>"),
    ("fake_system_tag", r"</?\s*(?:system|instructions?|admin|assistant)\s*>"),
    ("privilege_escalation", r"\b(?:developer|debug|god)\s+mode\b|\bDAN\b|\bsudo\b"),
]

_COMPILED_PATTERNS = [
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in INJECTION_PATTERNS
]

_DELIMITER_ESCAPE = re.compile(rf"</?\s*{UNTRUSTED_BLOCK_TAG}\s*>", re.IGNORECASE)


def _strip_control_characters(text: str) -> Tuple[str, int]:
    """Remove Cc/Cf characters, folding meaningful whitespace to a space."""
    out: List[str] = []
    stripped = 0
    for char in text:
        if char in _MEANINGFUL_WHITESPACE:
            out.append(" ")
            stripped += 1
            continue
        if unicodedata.category(char) in ("Cc", "Cf"):
            stripped += 1
            continue
        out.append(char)
    return "".join(out), stripped


def sanitize_customer_note(raw: Optional[str]) -> Tuple[str, SanitizationReport]:
    """Turn raw customer free-text into `untrusted_customer_note`.

    Returns the sanitized string and a report of what was done to it. Never
    raises: a customer note is data, and malformed data must not be able to
    break the pipeline for that event.
    """
    if raw is None:
        return "", SanitizationReport(
            original_length=0,
            sanitized_length=0,
            truncated=False,
            control_characters_stripped=0,
            injection_patterns_flagged=[],
            looks_like_instruction=False,
        )

    original_length = len(raw)

    # 1. strip control + format characters
    cleaned, stripped_count = _strip_control_characters(raw)

    # 2. neutralise any attempt to close the untrusted block early. This is the
    #    one pattern that is removed rather than merely flagged -- leaving it in
    #    would let the note escape into the instruction region of the prompt.
    delimiter_escape_found = bool(_DELIMITER_ESCAPE.search(cleaned))
    if delimiter_escape_found:
        cleaned = _DELIMITER_ESCAPE.sub(" ", cleaned)

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    # 3. hard cap AFTER stripping, so padding with invisible characters cannot
    #    push real content past the cap
    truncated = len(cleaned) > MAX_NOTE_LENGTH
    if truncated:
        cleaned = cleaned[:MAX_NOTE_LENGTH]

    # 4. flag instruction-like patterns (recorded, not removed)
    flagged = [name for name, pattern in _COMPILED_PATTERNS if pattern.search(cleaned)]
    if delimiter_escape_found and "delimiter_escape_attempt" not in flagged:
        flagged.append("delimiter_escape_attempt")

    report = SanitizationReport(
        original_length=original_length,
        sanitized_length=len(cleaned),
        truncated=truncated,
        control_characters_stripped=stripped_count,
        injection_patterns_flagged=sorted(flagged),
        looks_like_instruction=bool(flagged),
    )
    return cleaned, report
