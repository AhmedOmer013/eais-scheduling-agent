"""Tests that the production restaurant manifest loads correctly.

Distinct from `test_manifest.py` (which exercises the loader generically
against `tests/fixtures/manifests/`): this test targets the real,
production `eais_scheduling_agent/manifests/restaurant.yaml` shipped by
this task, demonstrating ARCHITECTURE.md's extension-point claim that
adding a sector means adding a manifest file and nothing else.
"""

from pathlib import Path

from eais_scheduling_agent.manifests.manifest import SectorManifest

RESTAURANT_MANIFEST_PATH = (
    Path(__file__).parent.parent
    / "eais_scheduling_agent"
    / "manifests"
    / "restaurant.yaml"
)


class TestProductionRestaurantManifest:
    def test_loads_and_produces_expected_sector_manifest(self):
        manifest = SectorManifest.load(str(RESTAURANT_MANIFEST_PATH))

        assert manifest.sector == "restaurant"
        assert manifest.enabled is True
        assert manifest.skill_pack == "restaurant_v1"
        assert manifest.approval_required_for == [
            "outside_working_hours",
            "double_booking",
            "missing_required_field",
        ]
