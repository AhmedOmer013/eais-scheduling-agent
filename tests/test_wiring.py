"""Tests for the shared wiring module (sector-naming, shared between
cli.py and http_api.py -- see docs/superpowers/specs/2026-08-05-http-interface-design.md).
"""

import os
from datetime import datetime

import pytest

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
from eais_scheduling_agent.intake.llm import OpenAICompatibleHTTPClient
from eais_scheduling_agent.manifests.manifest import ManifestValidationError
from eais_scheduling_agent.skillpacks.clinic import ClinicSkillPack
from eais_scheduling_agent.skillpacks.restaurant import RestaurantSkillPack


class TestBuildSkillPacks:
    def test_maps_clinic_and_restaurant_identifiers(self):
        packs = wiring.build_skill_packs()
        assert isinstance(packs["clinic_v1"], ClinicSkillPack)
        assert isinstance(packs["restaurant_v1"], RestaurantSkillPack)


class TestRenderConfirmation:
    def test_formats_template_with_request_fields(self):
        skill_pack = ClinicSkillPack()
        request = BookingRequest(
            sector="clinic",
            fields={
                "patient_name": "John Doe",
                "practitioner": "Dr. A",
                "start_time": datetime(2026, 8, 5, 10, 0, 0),
            },
            raw_text="Dr. A today at 10am, patient John Doe",
        )

        message = wiring.render_confirmation(skill_pack, request)

        assert message == "Confirmed: John Doe with Dr. A at 2026-08-05 10:00:00."


class TestLoadManifestForRender:
    def test_loads_real_clinic_manifest(self):
        manifest = wiring.load_manifest_for_render(str(wiring.DEFAULT_MANIFEST_DIR), "clinic")
        assert manifest.skill_pack == "clinic_v1"

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(ManifestValidationError):
            wiring.load_manifest_for_render(str(tmp_path), "veterinary")


class _CountingFakeIntake(IntakeService):
    def __init__(self):
        self.calls = 0

    def parse(self, text, sector):
        self.calls += 1
        return BookingRequest(sector=sector, fields={"call": self.calls}, raw_text=text)


class TestCachingIntake:
    def test_second_call_with_same_args_is_a_cache_hit(self):
        inner = _CountingFakeIntake()
        caching = wiring.CachingIntake(inner)

        first = caching.parse("some text", "clinic")
        second = caching.parse("some text", "clinic")

        assert first is second
        assert inner.calls == 1

    def test_different_args_are_not_cached_together(self):
        inner = _CountingFakeIntake()
        caching = wiring.CachingIntake(inner)

        caching.parse("text a", "clinic")
        caching.parse("text b", "clinic")

        assert inner.calls == 2


class TestBuildLLMClient:
    def test_defaults_when_no_env_vars_set(self, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert isinstance(client, OpenAICompatibleHTTPClient)
        assert client._url == "http://localhost:11434/v1/chat/completions"
        assert client._model == "llama3.2"
        assert client._api_key is None
        assert client._timeout == 60.0

    def test_reads_base_url_override(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_BASE_URL", "http://100.64.0.5:8000/v1")
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert client._url == "http://100.64.0.5:8000/v1/chat/completions"

    def test_reads_model_override(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert client._model == "Qwen/Qwen2.5-72B-Instruct"

    def test_reads_api_key_override(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_API_KEY", "secret-key")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert client._api_key == "secret-key"

    def test_reads_timeout_override_as_float(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_TIMEOUT", "120")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)

        client = wiring.build_llm_client()

        assert client._timeout == 120.0

    def test_invalid_timeout_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_TIMEOUT", "not-a-number")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)

        client = wiring.build_llm_client()

        assert client._timeout == 60.0


class TestResolveLLMConfig:
    def test_defaults_when_no_env_vars_set(self, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        config = wiring.resolve_llm_config()

        assert config == {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3.2",
            "api_key": None,
            "timeout": 60.0,
        }

    def test_reads_all_four_overrides(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_BASE_URL", "http://100.64.0.5:8000/v1")
        monkeypatch.setenv("EAIS_LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct")
        monkeypatch.setenv("EAIS_LLM_API_KEY", "secret-key")
        monkeypatch.setenv("EAIS_LLM_TIMEOUT", "120")

        config = wiring.resolve_llm_config()

        assert config == {
            "base_url": "http://100.64.0.5:8000/v1",
            "model": "Qwen/Qwen2.5-72B-Instruct",
            "api_key": "secret-key",
            "timeout": 120.0,
        }

    def test_invalid_timeout_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_TIMEOUT", "not-a-number")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)

        config = wiring.resolve_llm_config()

        assert config["timeout"] == 60.0
