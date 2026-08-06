"""Tests for the LLM-backed intake parser (T13).

Every test injects a fake `HTTPClient` callable directly into `LLMIntake`'s
constructor -- a plain Python function or small class defined right here in
the test file. None of them touch a real socket, `urllib`, or Ollama: per
the T13 brief, this environment (and CI) has no LLM runtime installed, and
the whole point of the injectable-client design in `intake/llm.py` is that
the test suite never needs one. `OpenAICompatibleHTTPClient` (the real
implementation) is constructed directly in `TestOpenAICompatibleHTTPClient`
below, but every one of those tests monkeypatches `urllib.request.urlopen`
first -- no test in this file ever opens a real socket.

Covers, per the brief's list:
    1. Well-formed response, all fields correct -> parsed, fallback untouched.
    2. Response missing some fields -> those keys absent, not `None`.
    3. Response with a wrong-typed field -> that field dropped, others kept.
    4. Client raises (connection error) -> fallback called, its result returned.
    5. Client returns non-JSON text -> same fallback behavior.
    6. Client returns valid JSON but empty/wrong-shaped (`{}`, a JSON array)
       -> also falls back (documented choice, see `_parse_and_validate`'s
       docstring in `intake/llm.py`: an empty/non-object response is treated
       as "unreliable", the same bucket as a network failure -- not as
       "confidently extracted zero fields").
    7. Fallback-called assertions use a spy `IntakeService` and check it was
       invoked with the exact same `text`/`sector` the caller supplied, not
       just that some result came back.
"""

import json
import urllib.request
from datetime import datetime

import pytest

from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.intake.llm import LLMIntake, OpenAICompatibleHTTPClient, _build_prompt


class _FakeHTTPResponse:
    """Minimal stand-in for the context-manager object `urlopen()` returns."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestOpenAICompatibleHTTPClient:
    """Tests the real HTTPClient implementation directly, via a monkeypatched
    `urllib.request.urlopen` -- no real socket, loopback or otherwise, per
    this plan's Global Constraints.
    """

    def test_posts_to_chat_completions_with_messages_shape(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(
            base_url="http://example.invalid/v1", model="test-model", timeout=42.0
        )

        client("some prompt")

        assert captured["url"] == "http://example.invalid/v1/chat/completions"
        assert captured["method"] == "POST"
        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["messages"] == [{"role": "user", "content": "some prompt"}]
        assert captured["body"]["response_format"] == {"type": "json_object"}
        assert captured["timeout"] == 42.0

    def test_extracts_content_from_choices_message(self, monkeypatch):
        def fake_urlopen(request, timeout):
            return _FakeHTTPResponse(
                json.dumps(
                    {"choices": [{"message": {"content": '{"practitioner": "Dr. A"}'}}]}
                ).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(base_url="http://example.invalid/v1", model="m")

        result = client("prompt")

        assert result == '{"practitioner": "Dr. A"}'

    def test_no_authorization_header_when_api_key_not_given(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(base_url="http://example.invalid/v1", model="m")

        client("prompt")

        assert captured["authorization"] is None

    def test_authorization_bearer_header_when_api_key_given(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(
            base_url="http://example.invalid/v1", model="m", api_key="secret-key"
        )

        client("prompt")

        assert captured["authorization"] == "Bearer secret-key"

    def test_trailing_slash_on_base_url_does_not_double_up(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(base_url="http://example.invalid/v1/", model="m")

        client("prompt")

        assert captured["url"] == "http://example.invalid/v1/chat/completions"


class _FakeClient:
    """A fake `HTTPClient`: returns a canned string or raises a canned error."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._error is not None:
            raise self._error
        return self._response


class _SpyFallback(IntakeService):
    """A fake `IntakeService` fallback that records how it was called."""

    def __init__(self):
        self.calls = []
        self.sentinel = BookingRequest(
            sector="__fallback__", fields={"from_fallback": True}, raw_text="__fallback__"
        )

    def parse(self, text: str, sector: str) -> BookingRequest:
        self.calls.append((text, sector))
        return self.sentinel


class TestLLMIntakeSatisfiesInterface:
    def test_is_an_intake_service(self):
        intake = LLMIntake(fallback=_SpyFallback(), client=_FakeClient(response="{}"))
        assert isinstance(intake, IntakeService)


class TestWellFormedResponse:
    def test_all_fields_correct_are_extracted_and_fallback_not_called(self):
        fallback = _SpyFallback()
        client = _FakeClient(
            response=json.dumps(
                {
                    "practitioner": "Dr. Chen",
                    "start_time": "2026-08-14T15:00:00",
                }
            )
        )
        intake = LLMIntake(fallback=fallback, client=client)

        request = intake.parse("Can I book Dr. Chen for Friday 3pm?", "clinic")

        assert isinstance(request, BookingRequest)
        assert request.sector == "clinic"
        assert request.raw_text == "Can I book Dr. Chen for Friday 3pm?"
        assert request.fields == {
            "practitioner": "Dr. Chen",
            "start_time": datetime(2026, 8, 14, 15, 0, 0),
        }
        assert fallback.calls == []

    def test_restaurant_fields_correct_are_extracted(self):
        fallback = _SpyFallback()
        client = _FakeClient(
            response=json.dumps(
                {
                    "party_size": 3,
                    "seating_preference": "outdoor",
                    "occasion": "birthday",
                }
            )
        )
        intake = LLMIntake(fallback=fallback, client=client)

        request = intake.parse("table for 3 outdoor, it's a birthday", "restaurant")

        assert request.fields == {
            "party_size": 3,
            "seating_preference": "outdoor",
            "occasion": "birthday",
        }
        assert fallback.calls == []

    def test_sector_and_raw_text_carried_through_unchanged(self):
        client = _FakeClient(response=json.dumps({"party_size": 2}))
        intake = LLMIntake(fallback=_SpyFallback(), client=client)
        text = "table for two please"

        request = intake.parse(text, "restaurant")

        assert request.sector == "restaurant"
        assert request.raw_text == text


class TestPartiallyOmittedResponse:
    def test_model_omitted_fields_are_absent_not_none(self):
        # The model correctly followed the "omit, don't guess" instruction
        # and only returned the practitioner, nothing about start_time.
        client = _FakeClient(response=json.dumps({"practitioner": "Dr. Salem"}))
        intake = LLMIntake(fallback=_SpyFallback(), client=client)

        request = intake.parse("I need to see Dr. Salem sometime", "clinic")

        assert request.fields == {"practitioner": "Dr. Salem"}
        assert "start_time" not in request.fields
        assert "patient_name" not in request.fields


class TestWrongTypedField:
    def test_wrong_typed_field_is_dropped_others_kept(self):
        fallback = _SpyFallback()
        client = _FakeClient(
            response=json.dumps(
                {
                    "party_size": "four",  # wrong type -- should be int
                    "seating_preference": "patio",
                }
            )
        )
        intake = LLMIntake(fallback=fallback, client=client)

        request = intake.parse("table for four on the patio", "restaurant")

        assert "party_size" not in request.fields
        assert request.fields == {"seating_preference": "patio"}
        # A single bad field is not a "response unreliable" event -- the
        # rest of the response is still usable, so the fallback is not
        # invoked.
        assert fallback.calls == []

    def test_wrong_typed_start_time_is_dropped(self):
        client = _FakeClient(
            response=json.dumps({"practitioner": "Dr. Lee", "start_time": "not-a-datetime"})
        )
        intake = LLMIntake(fallback=_SpyFallback(), client=client)

        request = intake.parse("see Dr. Lee whenever", "clinic")

        assert request.fields == {"practitioner": "Dr. Lee"}

    def test_unknown_field_name_is_dropped(self):
        client = _FakeClient(
            response=json.dumps({"practitioner": "Dr. Lee", "favorite_color": "blue"})
        )
        intake = LLMIntake(fallback=_SpyFallback(), client=client)

        request = intake.parse("see Dr. Lee", "clinic")

        assert request.fields == {"practitioner": "Dr. Lee"}


class TestClientRaisesFallsBack:
    def test_connection_error_falls_back_and_returns_fallback_result(self):
        fallback = _SpyFallback()
        client = _FakeClient(error=ConnectionRefusedError("Ollama not running"))
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("book Dr. Salem tomorrow", "clinic")

        assert result is fallback.sentinel
        assert fallback.calls == [("book Dr. Salem tomorrow", "clinic")]

    def test_timeout_falls_back(self):
        fallback = _SpyFallback()
        client = _FakeClient(error=TimeoutError("timed out"))
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("table for 2 tonight", "restaurant")

        assert result is fallback.sentinel
        assert fallback.calls == [("table for 2 tonight", "restaurant")]

    def test_does_not_raise_out_of_parse(self):
        # The core requirement of the fallback contract: whatever goes
        # wrong with the LLM call, parse() itself never raises.
        client = _FakeClient(error=RuntimeError("non-2xx response"))
        intake = LLMIntake(fallback=_SpyFallback(), client=client)

        # Should not raise.
        intake.parse("anything", "clinic")


class TestNonJSONResponseFallsBack:
    def test_plain_text_response_falls_back(self):
        fallback = _SpyFallback()
        client = _FakeClient(response="Sure! I'd be happy to help you book that.")
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("book Dr. Salem tomorrow", "clinic")

        assert result is fallback.sentinel
        assert fallback.calls == [("book Dr. Salem tomorrow", "clinic")]

    def test_truncated_json_falls_back(self):
        fallback = _SpyFallback()
        client = _FakeClient(response='{"practitioner": "Dr. Salem"')  # missing close brace
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("book Dr. Salem tomorrow", "clinic")

        assert result is fallback.sentinel
        assert fallback.calls == [("book Dr. Salem tomorrow", "clinic")]


class TestEmptyOrWrongShapeResponseFallsBack:
    def test_empty_object_falls_back(self):
        fallback = _SpyFallback()
        client = _FakeClient(response="{}")
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("mumble mumble", "clinic")

        assert result is fallback.sentinel
        assert fallback.calls == [("mumble mumble", "clinic")]

    def test_json_array_falls_back(self):
        fallback = _SpyFallback()
        client = _FakeClient(response=json.dumps(["Dr. Salem", "tomorrow"]))
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("book Dr. Salem tomorrow", "clinic")

        assert result is fallback.sentinel
        assert fallback.calls == [("book Dr. Salem tomorrow", "clinic")]

    def test_json_scalar_falls_back(self):
        fallback = _SpyFallback()
        client = _FakeClient(response=json.dumps("just a string"))
        intake = LLMIntake(fallback=fallback, client=client)

        result = intake.parse("book Dr. Salem tomorrow", "clinic")

        assert result is fallback.sentinel
        assert fallback.calls == [("book Dr. Salem tomorrow", "clinic")]


class TestFallbackInvokedWithSameArguments:
    """Directly asserts the fallback receives the identical text/sector,
    not just that *a* result comes back -- per the brief's explicit ask.
    """

    def test_fallback_receives_exact_text_and_sector_on_client_error(self):
        fallback = _SpyFallback()
        client = _FakeClient(error=OSError("network unreachable"))
        intake = LLMIntake(fallback=fallback, client=client)
        text = "urgent - need to see any available doctor today"

        intake.parse(text, "clinic")

        assert len(fallback.calls) == 1
        called_text, called_sector = fallback.calls[0]
        assert called_text == text
        assert called_sector == "clinic"

    def test_fallback_receives_exact_text_and_sector_on_bad_json(self):
        fallback = _SpyFallback()
        client = _FakeClient(response="not json at all")
        intake = LLMIntake(fallback=fallback, client=client)
        text = "table 4 2nite 8"

        intake.parse(text, "restaurant")

        assert fallback.calls == [(text, "restaurant")]


class TestPromptBuildsWithoutNetwork:
    """Prompt construction is a pure function -- sanity-check it directly,
    independent of any client, to confirm it stays free of I/O.
    """

    def test_prompt_includes_the_input_text_and_sector(self):
        prompt = _build_prompt("table for 4 tomorrow at 8pm", "restaurant")
        assert "table for 4 tomorrow at 8pm" in prompt
        assert "restaurant" in prompt

    def test_prompt_includes_few_shot_examples(self):
        prompt = _build_prompt("anything", "clinic")
        assert "Dr. Chen" in prompt
        assert "practitioner" in prompt


class TestUnknownSectorDoesNotCrash:
    def test_unrecognized_sector_still_returns_a_booking_request(self):
        client = _FakeClient(response=json.dumps({"start_time": "2026-08-14T15:00:00"}))
        intake = LLMIntake(fallback=_SpyFallback(), client=client)

        request = intake.parse("some text", "unknown_sector")

        assert request.sector == "unknown_sector"
        assert request.fields == {"start_time": datetime(2026, 8, 14, 15, 0, 0)}

    def test_unrecognized_sector_drops_sector_specific_fields(self):
        # party_size/practitioner are not in the base validator set used
        # for sectors other than clinic/restaurant.
        client = _FakeClient(
            response=json.dumps({"party_size": 4, "time_period": "evening"})
        )
        intake = LLMIntake(fallback=_SpyFallback(), client=client)

        request = intake.parse("some text", "unknown_sector")

        assert request.fields == {"time_period": "evening"}
