"""Sanitiser for untrusted customer free-text.

Direct and indirect prompt injection is the primary risk when customer-supplied
text reaches a model prompt, so everything crosses this boundary first. It is a
real, callable step with concrete rules rather than a prompt instruction or a
vague "sanitise" comment.

Four concrete rules, in order:

1. **Strip control characters.** Everything in Unicode categories Cc (control)
   and Cf (format) is removed. Cf matters as much as Cc -- it covers the
   bidirectional overrides (U+202A-U+202E, U+2066-U+2069) and zero-width joiners
   that are used to hide instruction text from a human reviewer while leaving it
   perfectly readable to a model. Tab/newline/carriage-return are folded to a
   single space first so words do not get glued together.

2. **Redact personal data.** Email addresses, UPI IDs, phone numbers, card
   numbers, PAN numbers and other long digit runs (account or Aadhaar
   numbers) are replaced with a typed placeholder such as `[REDACTED_EMAIL]`.
   The note is sent to a third-party model provider, and nothing in a
   recommendation depends on those values. Applied after control characters
   are stripped, so a zero-width character cannot split a number to slip it
   past the patterns. The report records which kinds were redacted, never the
   values.

3. **Truncate to a hard 500-character cap.** Applied after stripping, so a
   payload cannot pad itself past the cap with invisible characters. This
   mitigates both JSON-breaking payloads and context flooding.

4. **Flag instruction-like patterns.** Matches are recorded by name, not
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
        r"\b(?:approve|authorize|authorise|issue|grant|process|retry)\s+(?:the\s+|a\s+|my\s+)?"
        r"(?:refund|payment|discount|retry|charge)\b",
    ),
    ("override_directive", r"\boverride\b|\bbypass\b|\bignore\s+the\s+polic"),
    ("delimiter_escape_attempt", rf"</?\s*{UNTRUSTED_BLOCK_TAG}\s*>"),
    ("fake_system_tag", r"</?\s*(?:system|instructions?|admin|assistant)\s*>"),
    ("privilege_escalation", r"\b(?:developer|debug|god)\s+mode\b|\bDAN\b|\bsudo\b"),
    (
        "fake_authority_preamble",
        r"\b(?:system|admin|developer)\s*(?:message|note|instruction)\b\s*:",
    ),
    (
        "disregard_evidence_directive",
        r"\bregardless\s+of\s+(?:the\s+)?(?:trace|evidence|tracer|data|facts)\b",
    ),
]

_COMPILED_PATTERNS = [
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in INJECTION_PATTERNS
]

#: Personal-data patterns, most specific first. Each match becomes
#: `[REDACTED_<KIND>]`. Digit runs must be 9+ digits, so amounts and dates
#: are left alone.
PII_PATTERNS: List[Tuple[str, str]] = [
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    # A UPI virtual payment address: handle@provider, with no dot-TLD.
    ("upi_id", r"\b[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}\b"),
    ("pan", r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    # Indian mobile numbers: optional +91/91/0 prefix, then 10 digits from 6-9.
    ("phone", r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
    # 13-19 digits, optionally grouped by spaces or hyphens.
    ("card_number", r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"),
    # Any remaining run of 9+ digits (account numbers, Aadhaar, and the like).
    ("long_number", r"(?<!\d)\d(?:[ -]?\d){8,}(?!\d)"),
]

_COMPILED_PII = [(kind, re.compile(pattern)) for kind, pattern in PII_PATTERNS]


def redact_personal_data(text: str) -> Tuple[str, List[str]]:
    """Replace personal data with typed placeholders.

    Returns the redacted text and the sorted kinds found, never the values.
    """
    found: List[str] = []
    for kind, pattern in _COMPILED_PII:
        text, count = pattern.subn(f"[REDACTED_{kind.upper()}]", text)
        if count:
            found.append(kind)
    return text, sorted(found)

_DELIMITER_ESCAPE = re.compile(rf"</?\s*{UNTRUSTED_BLOCK_TAG}\s*>", re.IGNORECASE)


def _strip_control_characters(text: str) -> Tuple[str, int]:
    """Remove Cc/Cf characters, folding meaningful whitespace to a space.

    Folded whitespace is kept as a space and not counted as stripped.
    """
    out: List[str] = []
    stripped = 0
    for char in text:
        if char in _MEANINGFUL_WHITESPACE:
            out.append(" ")
            continue
        if unicodedata.category(char) in ("Cc", "Cf"):
            stripped += 1
            continue
        out.append(char)
    return "".join(out), stripped


def clean_trace_text(text: str) -> str:
    """Make tracer-derived text safe to place in the instruction region.

    Strips control/format characters and the untrusted-block delimiter. No
    truncation or flagging, so the root cause stays a verbatim quote. Error
    fields such as `reason` arrive over the wire in a real integration.
    """
    cleaned, _ = _strip_control_characters(text)
    return _DELIMITER_ESCAPE.sub(" ", cleaned)


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
            pii_redacted=[],
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

    # 3. redact personal data before the note can reach a model provider
    cleaned, pii_kinds = redact_personal_data(cleaned)

    # 4. hard cap AFTER stripping, so padding with invisible characters cannot
    #    push real content past the cap
    truncated = len(cleaned) > MAX_NOTE_LENGTH
    if truncated:
        cleaned = cleaned[:MAX_NOTE_LENGTH]

    # 5. flag instruction-like patterns (recorded, not removed)
    flagged = [name for name, pattern in _COMPILED_PATTERNS if pattern.search(cleaned)]
    if delimiter_escape_found and "delimiter_escape_attempt" not in flagged:
        flagged.append("delimiter_escape_attempt")

    report = SanitizationReport(
        original_length=original_length,
        sanitized_length=len(cleaned),
        truncated=truncated,
        control_characters_stripped=stripped_count,
        pii_redacted=pii_kinds,
        injection_patterns_flagged=sorted(flagged),
        looks_like_instruction=bool(flagged),
    )
    return cleaned, report
