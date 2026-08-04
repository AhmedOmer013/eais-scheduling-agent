"""Deterministic, offline, regex-based intake (T12).

`OfflineIntake` implements `IntakeService` (see `core.interfaces`) using
only `re` and `datetime` from the standard library -- no LLM, no network
calls, no third-party dependencies. It is the FR4/AC7 baseline: a rule-
based parser that behaves identically given identical input, and that
degrades honestly (omits a field) rather than guessing when it cannot
confidently extract something.

Scope, deliberately bounded (see the T12 brief -- "Realistic scope"):
this is not a general NLU system. It covers the date/time, practitioner/
patient, and party-size/customer vocabulary exercised by
`training/clinic_examples.jsonl` and `training/restaurant_examples.jsonl`,
plus a few narrow, low-risk "optional extra field" patterns (time period,
seating preference, occasion, urgency, action). It is not expected to
solve arbitrary free text -- `rest-10`'s heavy shorthand ("table 4 2nite
8") is the training data's own documented case where a deterministic
offline parser may reasonably fail closed (omit everything rather than
guess); T13's LLM-backed intake is explicitly allowed to do better there.

The "now" problem (AC7 determinism):
    Several training examples use relative dates ("tomorrow", "next
    Tuesday", "today") that can only be resolved against a reference
    "current time". Calling `datetime.now()` directly inside `parse()`
    would make the same input text resolve to a different date depending
    on which real day the parser runs -- which would make this module's
    own determinism tests flaky, and arguably violates the spirit of
    "identical input -> identical output" even though `IntakeService.
    parse`'s signature has no room for a "now" parameter (it is fixed by
    the abstract interface -- see `core.interfaces.IntakeService`).
    Resolved via the constructor instead, the same pattern T5/T7/T8/T9/
    T10 used for their own configuration: `OfflineIntake(now=...)` takes
    an optional zero-argument callable returning a `datetime`, defaulting
    to the real `datetime.now` for production use. Tests inject a fixed
    callable so every run resolves relative dates against the exact same
    reference point.

Per-sector dispatch:
    Per ARCHITECTURE.md's extension-point map, `intake/` is explicitly
    *not* held to `core/`'s "zero sector-awareness" rule -- unlike
    anything under `core/`, a small, honest per-sector dispatch here
    (different extraction logic for "practitioner name" vs. "party size")
    is expected and fine. `sector` is used only to select which
    sector-specific fields to attempt; the shared date/time extraction
    runs for every sector.

The "omit, don't guess" contract:
    Per `IntakeService.parse`'s docstring, a field this module cannot
    confidently extract is left out of `BookingRequest.fields` entirely
    -- never set to `None` or a guessed placeholder. This is what lets
    the already-built core (T6) and gate (T7) do their job: a missing key
    reads as "not extracted" and escalates to `PENDING_APPROVAL` with a
    specific reason, exactly as designed. This module's only job is
    extraction; it never invents a fallback date, time, practitioner, or
    party size.
"""

import re
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest

# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_WEEKDAY_INDEX = {name: idx for idx, name in enumerate(_WEEKDAY_NAMES)}
_WEEKDAY_ALT = r"(" + "|".join(_WEEKDAY_NAMES) + r")"

# Checked in this priority order by `_extract_date` -- most specific
# qualifier first, falling back to a bare weekday name last. This order
# matters: "next Tuesday" must not be caught by the bare-weekday pattern
# first, which would silently drop the "next" qualifier.
_NEXT_WEEKDAY_RE = re.compile(r"\bnext\s+" + _WEEKDAY_ALT + r"\b", re.IGNORECASE)
_THIS_WEEKDAY_RE = re.compile(r"\bthis\s+" + _WEEKDAY_ALT + r"\b", re.IGNORECASE)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.IGNORECASE)
_TODAY_TONIGHT_RE = re.compile(r"\b(today|tonight)\b", re.IGNORECASE)
_BARE_WEEKDAY_RE = re.compile(r"\b" + _WEEKDAY_ALT + r"\b", re.IGNORECASE)


def _resolve_weekday(today: date, weekday_idx: int, *, next_week: bool) -> date:
    """Resolve a weekday name to a concrete date relative to `today`.

    "this <weekday>" / a bare weekday name both resolve to the nearest
    occurrence of that weekday on or after `today` (today itself counts,
    if today happens to be that weekday). "next <weekday>" resolves to
    that same nearest occurrence, plus a further 7 days -- a documented,
    deliberately simple convention chosen to avoid the genuine ambiguity
    of colloquial "next X" (which English speakers use inconsistently),
    not a claim that it is the only reasonable reading.
    """
    days_ahead = (weekday_idx - today.weekday()) % 7
    resolved = today + timedelta(days=days_ahead)
    if next_week:
        resolved += timedelta(days=7)
    return resolved


def _extract_date(text: str, today: date) -> Optional[date]:
    """Extract a calendar date from free text, or `None` if unresolvable.

    Deliberately does NOT resolve vague phrases like "next week" or
    "sometime" to any date -- only an explicit weekday name or one of
    today/tomorrow/tonight resolves. `clinic-04` ("sometime next week")
    is expected to fall through to `None` here, not guess a date.
    """
    match = _NEXT_WEEKDAY_RE.search(text)
    if match:
        return _resolve_weekday(today, _WEEKDAY_INDEX[match.group(1).lower()], next_week=True)

    match = _THIS_WEEKDAY_RE.search(text)
    if match:
        return _resolve_weekday(today, _WEEKDAY_INDEX[match.group(1).lower()], next_week=False)

    if _TOMORROW_RE.search(text):
        return today + timedelta(days=1)

    if _TODAY_TONIGHT_RE.search(text):
        return today

    match = _BARE_WEEKDAY_RE.search(text)
    if match:
        return _resolve_weekday(today, _WEEKDAY_INDEX[match.group(1).lower()], next_week=False)

    return None


# ---------------------------------------------------------------------------
# Time extraction
# ---------------------------------------------------------------------------

# 12-hour clock with required am/pm, e.g. "9am", "3pm", "10:30am", "9:45pm".
_TIME_12H_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5][0-9]))?\s*([AaPp][Mm])\b")
# 24-hour clock, e.g. "20:00" -- only tried if no am/pm form matched, so an
# am/pm time is never re-parsed by this pattern.
_TIME_24H_RE = re.compile(r"\b([01][0-9]|2[0-3]):([0-5][0-9])\b")

_TIME_PERIOD_RE = re.compile(r"\b(morning|afternoon|evening|lunch|dinner)\b", re.IGNORECASE)


def _extract_time(text: str) -> Optional[time]:
    """Extract a clock time from free text, or `None` if unresolvable.

    Vague period words ("morning", "evening", "lunch") are NOT resolved
    to any clock time here -- see `_extract_time_period` for how those
    are carried through as a separate, honestly-labelled hint instead of
    a guessed time.
    """
    match = _TIME_12H_RE.search(text)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2)) if match.group(2) else 0
        if match.group(3).lower() == "pm":
            hour += 12
        return time(hour, minute)

    match = _TIME_24H_RE.search(text)
    if match:
        return time(int(match.group(1)), int(match.group(2)))

    return None


def _extract_time_period(text: str) -> Optional[str]:
    """Extract a vague time-of-day period word, lower-cased, or `None`."""
    match = _TIME_PERIOD_RE.search(text)
    return match.group(1).lower() if match else None


def _extract_start_time(text: str, today: date) -> Optional[datetime]:
    """Combine an extracted date and time into `start_time`.

    Per the brief: `start_time` is set only when BOTH a date and a clock
    time were successfully extracted. If either is missing or
    unresolvable, `start_time` is omitted entirely -- no default date, no
    default time is ever guessed.
    """
    resolved_date = _extract_date(text, today)
    resolved_time = _extract_time(text)
    if resolved_date is not None and resolved_time is not None:
        return datetime.combine(resolved_date, resolved_time)
    return None


# ---------------------------------------------------------------------------
# Clinic-specific extraction
# ---------------------------------------------------------------------------

# "Dr Salem", "Dr. Salem", "dr. chen" -- optional period, one or more
# spaces, then a name token. Deliberately requires the "Dr" title itself;
# a generic "the doctor" / "any available doctor" does not match, which is
# the intended degrade-to-omitted behaviour for clinic-03/clinic-08.
_PRACTITIONER_RE = re.compile(r"\bDr\.?\s+([A-Za-z]+)", re.IGNORECASE)

# Narrow, explicit "patient <Name>" pattern -- e.g. clinic-10's
# "Dr. Salem, patient Ahmed Omer, ...". Requires the literal word
# "patient" immediately before the name; does NOT attempt to resolve
# referential phrases like "my son" (clinic-07's ambiguous_patient case)
# -- those are intentionally left unmatched so patient_name is omitted
# rather than storing a non-name reference.
_PATIENT_NAME_RE = re.compile(r"\bpatient\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\b")

_ACTION_RE = re.compile(r"\b(reschedule|cancel)\b", re.IGNORECASE)
_URGENCY_RE = re.compile(r"\b(urgent|asap)\b", re.IGNORECASE)


def _extract_practitioner(text: str) -> Optional[str]:
    """Extract a "Dr. <Name>" practitioner name, normalized to "Dr. Name"."""
    match = _PRACTITIONER_RE.search(text)
    return f"Dr. {match.group(1).capitalize()}" if match else None


def _extract_patient_name(text: str) -> Optional[str]:
    match = _PATIENT_NAME_RE.search(text)
    return match.group(1) if match else None


def _extract_action(text: str) -> Optional[str]:
    """Detect a reschedule/cancel verb, lower-cased, or `None`.

    Booking mutation (reschedule/cancel) is out of scope for this
    prototype (see `IntakeService`'s docstring and `clinic-09`'s
    `unsupported_action` note) -- this module does not act on it. But
    carrying the detected verb through as an extra `action` field means
    the request is never silently misfiled as an ordinary fresh booking;
    it stays visible in `BookingRequest.fields` and the audit trail for
    whatever downstream handling is built later.
    """
    match = _ACTION_RE.search(text)
    return match.group(1).lower() if match else None


def _extract_urgency(text: str) -> Optional[str]:
    match = _URGENCY_RE.search(text)
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# Restaurant-specific extraction
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUMBER_TOKEN = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"

# Tried in order; first match wins. The brief names "table for", "party
# of", "group of", and a bare number near "people"/"guests" explicitly.
# The plain "for <N>" pattern is this module's own narrow addition, needed
# for phrasings like rest-05's "reserve outdoor seating for 3" and
# rest-04's "booking for 6 people" that don't literally contain "table
# for"/"party of"/"group of" -- validated against all 10 restaurant
# training examples without over-matching; kept last among the
# prefix-based patterns (checked after the more specific three) so a more
# specific phrase always wins if both are present in the same text.
_PARTY_SIZE_PATTERNS = (
    re.compile(r"\btable for\s+" + _NUMBER_TOKEN + r"\b", re.IGNORECASE),
    re.compile(r"\bparty of\s+" + _NUMBER_TOKEN + r"\b", re.IGNORECASE),
    re.compile(r"\bgroup of\s+" + _NUMBER_TOKEN + r"\b", re.IGNORECASE),
    re.compile(r"\bfor\s+" + _NUMBER_TOKEN + r"\b", re.IGNORECASE),
    re.compile(r"\b" + _NUMBER_TOKEN + r"\s+(?:people|guests)\b", re.IGNORECASE),
)

# Deliberately conservative: no training example needs this (none name the
# customer explicitly), so in practice `customer_name` is omitted almost
# everywhere, same story as `patient_name` -- see the brief's own note
# that this is expected and fine.
_CUSTOMER_NAME_RE = re.compile(
    r"\b(?:under|customer)\s+(?:the\s+name\s+)?(?:is\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\b"
)

# Small whitelist of concrete seating phrases actually seen in the
# training data -- not an attempt at general preference extraction.
_SEATING_RE = re.compile(r"\b(outdoor|indoor|patio|window seat|quiet corner)\b", re.IGNORECASE)
_OCCASION_RE = re.compile(r"\b(anniversary|birthday)\b", re.IGNORECASE)


def _extract_party_size(text: str) -> Optional[int]:
    for pattern in _PARTY_SIZE_PATTERNS:
        match = pattern.search(text)
        if match:
            token = match.group(1).lower()
            return _NUMBER_WORDS.get(token, None) if token in _NUMBER_WORDS else int(token)
    return None


def _extract_customer_name(text: str) -> Optional[str]:
    match = _CUSTOMER_NAME_RE.search(text)
    return match.group(1) if match else None


def _extract_seating_preference(text: str) -> Optional[str]:
    match = _SEATING_RE.search(text)
    return match.group(1).lower() if match else None


def _extract_occasion(text: str) -> Optional[str]:
    match = _OCCASION_RE.search(text)
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# OfflineIntake
# ---------------------------------------------------------------------------

_CLINIC_SECTOR = "clinic"
_RESTAURANT_SECTOR = "restaurant"


class OfflineIntake(IntakeService):
    """Deterministic, regex-based `IntakeService` implementation.

    Args:
        now: Optional zero-argument callable returning a `datetime`, used
            as the reference "current time" for resolving relative dates
            ("today", "tomorrow", "next Tuesday", ...). Defaults to the
            real `datetime.now` for production use. Tests should inject a
            fixed callable (e.g. ``lambda: datetime(2026, 8, 10, 12, 0)``)
            so the same input text always resolves to the same output --
            see the module docstring's "The 'now' problem" section for
            why this is a constructor argument rather than a `parse()`
            argument.
    """

    def __init__(self, now: Optional[Callable[[], datetime]] = None) -> None:
        self._now: Callable[[], datetime] = now if now is not None else datetime.now

    def parse(self, text: str, sector: str) -> BookingRequest:
        """Extract a `BookingRequest` from free text using regex rules only.

        See the module docstring for the omit-don't-guess contract this
        follows, and for how `sector` selects which extra extraction
        logic runs. `sector` and `raw_text` are always placed on the
        returned `BookingRequest` unchanged, per `IntakeService.parse`'s
        contract, regardless of how much (or how little) of `fields`
        could be extracted.
        """
        reference_now = self._now()
        fields: dict = {}

        resolved_date = _extract_date(text, reference_now.date())
        resolved_time = _extract_time(text)
        if resolved_date is not None and resolved_time is not None:
            fields["start_time"] = datetime.combine(resolved_date, resolved_time)
        elif resolved_time is None:
            period = _extract_time_period(text)
            if period is not None:
                fields["time_period"] = period

        if sector == _CLINIC_SECTOR:
            practitioner = _extract_practitioner(text)
            if practitioner is not None:
                fields["practitioner"] = practitioner

            patient_name = _extract_patient_name(text)
            if patient_name is not None:
                fields["patient_name"] = patient_name

        elif sector == _RESTAURANT_SECTOR:
            party_size = _extract_party_size(text)
            if party_size is not None:
                fields["party_size"] = party_size

            customer_name = _extract_customer_name(text)
            if customer_name is not None:
                fields["customer_name"] = customer_name

            seating_preference = _extract_seating_preference(text)
            if seating_preference is not None:
                fields["seating_preference"] = seating_preference

            occasion = _extract_occasion(text)
            if occasion is not None:
                fields["occasion"] = occasion

        # Sector-agnostic optional extras.
        action = _extract_action(text)
        if action is not None:
            fields["action"] = action

        urgency = _extract_urgency(text)
        if urgency is not None:
            fields["urgency"] = urgency

        return BookingRequest(sector=sector, fields=fields, raw_text=text)
