"""LLM-backed intake, with graceful fallback to a deterministic parser (T13).

`LLMIntake` implements `IntakeService` (see `core.interfaces`) by asking an
LLM -- local or hosted, resolved by the T13 brief and later generalized
beyond its original Ollama-only scope, not relitigated here -- to
extract structured fields from free text, then defensively validating
whatever comes back. Same interface as T12's `OfflineIntake`, and in
practice constructed *with* an `OfflineIntake` instance as its fallback:
T14 will decide which of the two gets wired into `SchedulingAgentCore`,
not this module.

Why this module can never be a single point of failure:

    This environment (and CI) has no LLM runtime installed -- see the T13
    brief's "Runtime choice" section. Even in a deployment that *does* have
    a local or hosted LLM server reachable, it is unreliable in ways a
    regex parser is not: the process might not be running, the model might
    not be pulled, the response might not be valid JSON, or the JSON might
    have the right shape but wrong-typed values. `parse()` treats every
    one of those as the same signal -- "the LLM path didn't work this time" -- and
    delegates to `self._fallback.parse(text, sector)` rather than raising.
    Nothing here ever raises out of `parse()` on account of the LLM; the
    only exceptions that can escape are bugs, not degraded LLM behaviour.

Two independently swappable layers:

    1. **The HTTP client** (`HTTPClient` / `OpenAICompatibleHTTPClient`) --
       the only part of this module that touches the network. `LLMIntake`
       accepts any zero-state callable `str -> str` (prompt in, model's
       raw text out) via its `client` constructor argument.
       `OpenAICompatibleHTTPClient` is the real implementation, speaking
       the OpenAI-compatible `/chat/completions` shape that both a local
       Ollama server and a hosted vLLM server understand -- which backend
       it talks to is purely a matter of which `base_url`/`model`/`api_key`
       it was constructed with (see `wiring.build_llm_client`), never a
       code difference. Tests inject a fake callable instead; see
       `tests/test_llm_intake.py`. This is what makes "zero real network
       calls in tests" possible without a mocking framework: dependency
       injection, not patching.
    2. **Response validation** (`_parse_and_validate`) -- independent of
       the client. Given the client's raw text, this parses it as JSON and
       validates each field's type against a per-sector schema, dropping
       (not guessing, not crashing on) anything malformed. This is what
       makes "do not trust the model to comply with the prompt's own
       instructions" real rather than aspirational.

The "omit, don't guess" contract (same as T12):
    A field the model didn't return, or returned in a form that fails
    validation, is left out of `BookingRequest.fields` entirely -- never
    set to `None`. See `intake/offline.py`'s module docstring for why this
    matters to the rest of the system (the core's missing-field check is a
    presence check, not a truthiness check).
"""

import json
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

# A client is any zero-state callable taking the fully-built prompt and
# returning the model's raw text response. It may raise anything on
# failure (connection refused, timeout, non-2xx status, an unparseable
# envelope) -- `LLMIntake.parse` treats every exception from the client
# identically (see the module docstring), so the client does not need its
# own error-normalization layer.
HTTPClient = Callable[[str], str]


class OpenAICompatibleHTTPClient:
    """Real `HTTPClient`: POSTs to any OpenAI-compatible `/chat/completions` endpoint.

    Both a local Ollama server (recent versions expose an OpenAI-compatible
    API alongside their native one) and a hosted vLLM server serving a
    larger model use this same request/response shape -- "local" and
    "hosted" are the same code path here, differing only in which
    `base_url` / `model` / `api_key` are configured. See
    `wiring.build_llm_client`, the one place those are read from the
    environment.

    Uses stdlib `urllib.request` only -- no new dependency, same precedent
    as the client this replaces.

    Never constructed directly by tests -- tests inject their own fake
    `HTTPClient` callable, or monkeypatch `urllib.request.urlopen` to test
    this class's own request-building logic without a real socket. See
    `tests/test_llm_intake.py::TestOpenAICompatibleHTTPClient`.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def __call__(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                # Best-effort hint, same as the native client's "format":
                # "json" -- not relied upon; `_parse_and_validate` below
                # validates independently either way.
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        # Explicit User-Agent -- some hosted providers (Groq, confirmed)
        # front their API with Cloudflare, which 403s urllib's default
        # "Python-urllib/x.y" as bot traffic (Cloudflare error 1010). That
        # 403 is indistinguishable from any other client failure once
        # `LLMIntake.parse()` catches it, so without this the LLM path
        # would silently and permanently fall back to the offline parser
        # against any such provider.
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "eais-scheduling-agent/1.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers=headers,
            method="POST",
        )
        # Any failure here (connection refused, DNS failure, timeout, a
        # non-2xx status raised by urllib as HTTPError) propagates to the
        # caller unchanged -- `LLMIntake.parse` is the layer that decides
        # what to do about it.
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = response.read().decode("utf-8")
        envelope = json.loads(body)
        return envelope["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

# A hand-picked subset of `training/clinic_examples.jsonl` and
# `training/restaurant_examples.jsonl`'s `happy_path` / `preference_extra_
# field` examples, per the brief's explicit suggestion -- these anchor the
# model on the output schema and on "omit fields you're not sure of"
# without being an exhaustive rewrite of the training set. Output JSON
# below uses this module's own field names/types (`start_time` as ISO
# 8601, matching `_validate_start_time`), not the training files' own
# human-facing `expected` schema (`date`/`time` as separate strings) --
# the two are deliberately different, see `intake/offline.py`'s docstring
# for the same distinction on the offline side.
_FEW_SHOT_EXAMPLES = (
    (
        "clinic",
        "Can I book an appointment with Dr. Chen for this Friday at 3pm?",
        {"practitioner": "Dr. Chen", "start_time": "2026-08-14T15:00:00"},
    ),
    (
        "clinic",
        "Dr. Salem, patient Ahmed Omer, Tuesday 10:30am, annual physical",
        {
            "practitioner": "Dr. Salem",
            "patient_name": "Ahmed Omer",
            "start_time": "2026-08-11T10:30:00",
        },
    ),
    (
        "restaurant",
        "reserve outdoor seating for 3, sunday lunch",
        {"party_size": 3, "time_period": "lunch", "seating_preference": "outdoor"},
    ),
    (
        "restaurant",
        "need a table for our anniversary dinner, party of 2, saturday 7pm, "
        "quiet corner if possible",
        {
            "party_size": 2,
            "start_time": "2026-08-16T19:00:00",
            "seating_preference": "quiet corner",
            "occasion": "anniversary",
        },
    ),
)

_SCHEMA_DESCRIPTION = """\
Allowed fields and types, by sector:

clinic:
  practitioner (string, e.g. "Dr. Salem")
  patient_name (string)
  start_time (ISO 8601 datetime string, e.g. "2026-08-11T09:00:00")
  time_period (one of "morning", "afternoon", "evening", "lunch", "dinner")
  action (one of "reschedule", "cancel")
  urgency (one of "urgent", "asap")

restaurant:
  party_size (integer)
  customer_name (string)
  start_time (ISO 8601 datetime string, e.g. "2026-08-11T09:00:00")
  time_period (one of "morning", "afternoon", "evening", "lunch", "dinner")
  seating_preference (string, e.g. "outdoor", "quiet corner")
  occasion (string, e.g. "anniversary", "birthday")
"""


def _build_prompt(text: str, sector: str, reference_date: datetime) -> str:
    """Build the few-shot prompt sent to the model for one `parse()` call.

    Deliberately a pure function of `(text, sector, reference_date)` -- no
    I/O, no state -- so prompt construction itself is trivially unit-testable
    without an HTTP client at all. `reference_date` is caller-supplied (see
    `LLMIntake.__init__`'s `now`) rather than computed here, same reason
    `OfflineIntake` takes an injectable `now` instead of calling
    `datetime.now()` internally: deterministic tests, one real clock read
    per `parse()` call.

    Without `reference_date` in the prompt, the model has no notion of
    "today" at all and relative dates ("this Wednesday", "next Tuesday")
    are pure guesses -- confirmed wrong in manual testing against a real
    hosted model (resolved "this Wednesday" to a Thursday). Telling it the
    exact date and weekday turns that guess into arithmetic.
    """
    lines = [
        "You are an intake parser for a scheduling system. Extract structured "
        "booking fields from the user's free-text request and respond with a "
        "single JSON object, and nothing else -- no explanation, no markdown "
        "code fences.",
        "",
        "Only include a field if you are confident about its value. If you "
        "are not sure, or the input does not mention it, OMIT that key "
        "entirely -- do not guess, and do not use null/None as a placeholder.",
        "",
        f"Today's date is {reference_date.strftime('%Y-%m-%d')} "
        f"({reference_date.strftime('%A')}). Resolve relative dates "
        '("today", "tomorrow", "this Friday", "next Tuesday", ...) against '
        "this date -- compute them, do not guess.",
        "",
        _SCHEMA_DESCRIPTION,
        "Examples:",
    ]
    for example_sector, example_text, example_output in _FEW_SHOT_EXAMPLES:
        lines.append(f'Sector: {example_sector}\nInput: "{example_text}"')
        lines.append(f"Output: {json.dumps(example_output)}")
        lines.append("")
    lines.append("Now parse this request.")
    lines.append(f"Sector: {sector}")
    lines.append(f'Input: "{text}"')
    lines.append("Output:")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response validation -- defensive by design, see module docstring.
# ---------------------------------------------------------------------------

_TIME_PERIODS = {"morning", "afternoon", "evening", "lunch", "dinner"}
_ACTIONS = {"reschedule", "cancel"}
_URGENCIES = {"urgent", "asap"}


def _validate_nonempty_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _validate_enum(allowed: set) -> Callable[[Any], Optional[str]]:
    def _validator(value: Any) -> Optional[str]:
        if isinstance(value, str) and value.lower() in allowed:
            return value.lower()
        return None

    return _validator


def _validate_party_size(value: Any) -> Optional[int]:
    # `bool` is a subclass of `int` in Python -- explicitly excluded so a
    # stray `true`/`false` in the model's JSON doesn't masquerade as 1/0.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _validate_start_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# Fields available regardless of sector -- mirrors `OfflineIntake`'s own
# "sector-agnostic optional extras" (`start_time`/`time_period`/`action`/
# `urgency` are computed unconditionally there too; only the practitioner/
# party-size groups are sector-gated).
_BASE_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    "start_time": _validate_start_time,
    "time_period": _validate_enum(_TIME_PERIODS),
    "action": _validate_enum(_ACTIONS),
    "urgency": _validate_enum(_URGENCIES),
}

_CLINIC_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    **_BASE_VALIDATORS,
    "practitioner": _validate_nonempty_str,
    "patient_name": _validate_nonempty_str,
}

_RESTAURANT_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    **_BASE_VALIDATORS,
    "party_size": _validate_party_size,
    "customer_name": _validate_nonempty_str,
    "seating_preference": _validate_nonempty_str,
    "occasion": _validate_nonempty_str,
}

_CLINIC_SECTOR = "clinic"
_RESTAURANT_SECTOR = "restaurant"


def _validators_for(sector: str) -> Dict[str, Callable[[Any], Any]]:
    if sector == _CLINIC_SECTOR:
        return _CLINIC_VALIDATORS
    if sector == _RESTAURANT_SECTOR:
        return _RESTAURANT_VALIDATORS
    return _BASE_VALIDATORS


def _parse_and_validate(raw_response: str, sector: str) -> Optional[dict]:
    """Turn the model's raw text into a validated `fields` dict, or `None`.

    `None` is this function's way of saying "treat this as LLM-unavailable,
    fall back" -- returned when the text isn't JSON at all, or parses to
    something that isn't a non-empty JSON object. Per the brief: a
    completely unparseable/empty/wrong-shaped response is "unreliable",
    the same bucket as a network failure, not "confidently extracted zero
    fields". A non-empty object that merely contains some malformed field
    values is a different case -- handled by validating and dropping each
    field individually below, still returning a (possibly smaller) dict
    rather than `None`.
    """
    try:
        parsed = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict) or not parsed:
        return None

    validators = _validators_for(sector)
    fields: dict = {}
    for key, value in parsed.items():
        validator = validators.get(key)
        if validator is None:
            continue  # Unknown field name -- not part of this sector's
            # contract; dropped silently rather than passed through.
        normalized = validator(value)
        if normalized is not None:
            fields[key] = normalized
    return fields


# ---------------------------------------------------------------------------
# LLMIntake
# ---------------------------------------------------------------------------


class LLMIntake(IntakeService):
    """LLM-backed `IntakeService`, falling back to a deterministic parser.

    Args:
        fallback: An `IntakeService` to delegate to whenever the LLM path
            fails for any reason -- see the module docstring. In practice
            an `OfflineIntake` instance, though nothing here depends on
            that concretely; any `IntakeService` works. T14's job, not
            this module's, is to actually construct and wire one in.
        client: `HTTPClient` (prompt string in, model's raw text response
            out; may raise on failure). Required -- production code
            builds one explicitly via `wiring.build_llm_client()` (see
            `cli.py`/`http_api.py`); tests always pass their own fake
            callable so no test touches a real network -- see
            `tests/test_llm_intake.py`.
        now: Zero-arg callable returning the current `datetime`, used as
            `_build_prompt`'s `reference_date` so the model is told what
            day it actually is rather than guessing relative dates blind.
            Defaults to the real `datetime.now` for production use --
            same injectable-clock pattern as `OfflineIntake`'s `now`.
            Tests inject a fixed callable for determinism.
    """

    def __init__(
        self,
        fallback: IntakeService,
        client: HTTPClient,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._fallback = fallback
        self._client = client
        self._now = now

    def parse(self, text: str, sector: str) -> BookingRequest:
        """Extract a `BookingRequest` via the LLM, falling back on failure.

        See the module docstring for the full failure-handling contract.
        Nothing here raises on account of the LLM being unreachable,
        slow, or unreliable -- it always either returns a `BookingRequest`
        built from validated LLM output, or the fallback's result.
        """
        prompt = _build_prompt(text, sector, self._now())

        try:
            raw_response = self._client(prompt)
        except Exception:
            # Connection refused, timeout, non-2xx, a malformed envelope
            # from the client itself -- all treated identically as "the
            # LLM path isn't available right now".
            return self._fallback.parse(text, sector)

        fields = _parse_and_validate(raw_response, sector)
        if fields is None:
            return self._fallback.parse(text, sector)

        return BookingRequest(sector=sector, fields=fields, raw_text=text)
