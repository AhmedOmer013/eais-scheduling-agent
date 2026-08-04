"""JSON Lines audit trail implementation.

Provides a concrete AuditTrail that writes one JSON object per line,
with ISO 8601 datetime serialization, durable writes via open-per-call.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Union

from eais_scheduling_agent.core.interfaces import AuditTrail
from eais_scheduling_agent.core.models import AuditRecord


def _json_safe(value: Any) -> Any:
    """Recursively convert `datetime` values to ISO 8601 strings.

    `AuditRecord.intent` is `dict(request.fields)` (T6) -- a copy of
    whatever a skill pack's `BookingRequest.fields` contains. Every
    current skill pack (T5, T10) puts a real `datetime.datetime` under
    `start_time`, which `json.dumps` cannot serialize on its own. This
    walks the structure generically (not keyed on `"start_time"`
    specifically) so any future field of datetime type is handled the
    same way, without `core/audit.py` needing to know a sector's field
    names.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class JsonLinesAuditTrail(AuditTrail):
    """Records audit events as JSON Lines (one JSON object per line).

    Design choices:
    - Write durability: open-per-call (open, write complete line, close).
      This guarantees durability after each append() returns, with minimal
      performance cost for an audit trail. Avoids risk of partial/corrupt
      trailing lines if the process crashes.
    - Directory creation: parent directories are auto-created if missing.
      Raises a clear FileNotFoundError only if the parent directory path
      itself is invalid (e.g. a file exists where a directory is expected).

    Attributes:
        path: The file path to which audit records are appended.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        """Initialize the audit trail with a file path.

        Args:
            path: Where to write audit records. Parent directory will be
                created if it does not exist. The file itself will be created
                on the first append().
        """
        self.path = Path(path)

    def append(self, record: AuditRecord) -> None:
        """Append one audit record as a JSON Line.

        Serializes the AuditRecord to JSON with ISO 8601 datetime format,
        writes it as a single complete line (no partial writes), and ensures
        durability via open-per-call (file is closed after each write).

        Args:
            record: The audit record to append.

        Raises:
            FileNotFoundError: If the parent directory cannot be created
                (e.g. a file exists at a required directory path).
            IOError: If writing to the file fails (permission, disk full, etc).
        """
        # Ensure parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize the record to a JSON object
        json_obj = self._record_to_json(record)
        json_line = json.dumps(json_obj)

        # Write the complete line atomically: open, write newline, close.
        # This guarantees durability and avoids partial writes.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json_line + "\n")

    @staticmethod
    def _record_to_json(record: AuditRecord) -> dict:
        """Convert an AuditRecord to a JSON-serializable dict.

        Converts `timestamp` to an ISO 8601 string. `intent` is passed
        through `_json_safe` since it can itself contain `datetime`
        values (e.g. `start_time`) -- everything else on `AuditRecord`
        is already JSON-native (str, list of str).

        Args:
            record: The audit record to convert.

        Returns:
            A dict with the same structure as the record, ready to serialize
            to JSON.
        """
        return {
            "input": record.input,
            "skill_pack": record.skill_pack,
            "intent": _json_safe(record.intent),
            "rules_evaluated": record.rules_evaluated,
            "decision": record.decision,
            "approval_status": record.approval_status,
            "timestamp": record.timestamp.isoformat(),
        }
