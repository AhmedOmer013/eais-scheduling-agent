"""Tests for the `eais-book` CLI (T14 -- the wiring layer).

Invokes `cli.main()` programmatically with an explicit `argv` list (never a
real subprocess, and never patching `sys.argv`) against the **real**,
bundled `manifests/clinic.yaml` / `manifests/restaurant.yaml` and the
**real** `ClinicSkillPack` / `RestaurantSkillPack` defaults -- this is
deliberately an integration test of the actual wiring `main()` assembles,
not a test against fakes. `tmp_path` supplies the audit file path so no
test run pollutes the repo's own `audit.jsonl`.

Determinism note (see the T14 brief): every request text below uses "today
at <time>" rather than a weekday name or "tomorrow", so date resolution
never depends on which calendar day the suite happens to run on. Only the
*decision status* is asserted, never an exact resolved date.
"""

import json

from eais_scheduling_agent import cli


def _audit_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


class TestClinicConfirmed:
    """A well-formed clinic request, inside working hours, auto-confirms."""

    def test_prints_confirmation_and_writes_one_audit_line(self, tmp_path, capsys):
        audit_path = tmp_path / "audit.jsonl"

        exit_code = cli.main(
            [
                "clinic",
                "Dr. A today at 10am, patient John Doe",
                "--audit-file",
                str(audit_path),
            ]
        )

        assert exit_code == 0

        out = capsys.readouterr().out
        assert "John Doe" in out
        assert "Dr. A" in out
        assert out.strip().startswith("Confirmed:")

        lines = _audit_lines(audit_path)
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["decision"] == "CONFIRMED"
        assert record["approval_status"] == "not_required"


class TestRestaurantConfirmed:
    """A well-formed restaurant request, inside working hours, auto-confirms."""

    def test_prints_confirmation_and_writes_one_audit_line(self, tmp_path, capsys):
        audit_path = tmp_path / "audit.jsonl"

        exit_code = cli.main(
            [
                "restaurant",
                "table for 4 today at 6pm, customer Jane Smith",
                "--audit-file",
                str(audit_path),
            ]
        )

        assert exit_code == 0

        out = capsys.readouterr().out
        assert "Jane Smith" in out
        assert "4" in out
        assert out.strip().startswith("Confirmed:")

        lines = _audit_lines(audit_path)
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["decision"] == "CONFIRMED"
        assert record["approval_status"] == "not_required"


class TestPendingApproval:
    """A request outside working hours escalates rather than auto-confirming.

    `ClinicSkillPack.validate()` only checks time-of-day (not day-of-week,
    see the brief), so 6am is reliably outside the default 09:00-17:00
    clinic hours regardless of which day this test runs.
    """

    def test_prints_reason_and_writes_one_audit_line(self, tmp_path, capsys):
        audit_path = tmp_path / "audit.jsonl"

        exit_code = cli.main(
            [
                "clinic",
                "Dr. A today at 6am, patient John Doe",
                "--audit-file",
                str(audit_path),
            ]
        )

        # PENDING_APPROVAL is a successful run of the system, not a CLI
        # failure -- see the brief: "both are 'the system worked
        # correctly,' not failures."
        assert exit_code == 0

        out = capsys.readouterr().out
        assert "Pending approval" in out
        assert "outside working hours" in out

        lines = _audit_lines(audit_path)
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["decision"] == "PENDING_APPROVAL"
        assert record["approval_status"] == "pending"


class TestUnknownSector:
    """An unrecognized sector fails clearly, without a Python traceback."""

    def test_clear_error_nonzero_exit_no_traceback(self, tmp_path, capsys):
        audit_path = tmp_path / "audit.jsonl"

        exit_code = cli.main(
            [
                "veterinary",
                "some request text",
                "--audit-file",
                str(audit_path),
            ]
        )

        assert exit_code != 0

        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err
        assert "veterinary" in captured.err

        # No request was ever processed, so nothing should have been
        # audited -- see OrchestrationError's docstring in core/orchestrator.py.
        assert not audit_path.exists()


class TestLLMFlagFallsBackCleanly:
    """`--llm` with no LLM backend reachable still completes via fallback.

    No LLM runtime is installed in this environment (or CI) -- see
    `intake/llm.py`'s module docstring. This exercises the real,
    unmocked `--llm` wiring end to end and confirms it degrades to the
    same offline result rather than crashing or hanging.
    """

    def test_falls_back_to_offline_and_still_confirms(self, tmp_path, capsys, monkeypatch):
        # Clear any ambient EAIS_LLM_* config so this test always targets
        # the unreachable-by-default localhost endpoint, regardless of
        # what a developer running this suite locally has exported to
        # point `--llm` at their own real backend (exactly what this
        # branch exists to let them do).
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        audit_path = tmp_path / "audit.jsonl"

        exit_code = cli.main(
            [
                "clinic",
                "Dr. A today at 10am, patient John Doe",
                "--llm",
                "--audit-file",
                str(audit_path),
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert out.strip().startswith("Confirmed:")
        assert len(_audit_lines(audit_path)) == 1
