"""Tests for the shared wiring module (sector-naming, shared between
cli.py and http_api.py -- see docs/superpowers/specs/2026-08-05-http-interface-design.md).
"""

from datetime import datetime

import pytest

from eais_scheduling_agent import wiring
from eais_scheduling_agent.core.interfaces import IntakeService
from eais_scheduling_agent.core.models import BookingRequest
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
