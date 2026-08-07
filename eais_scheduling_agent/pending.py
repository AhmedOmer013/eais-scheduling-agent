"""File-backed queue of violation/conflict PENDING_APPROVAL requests
awaiting a human accept/reject decision (see
docs/superpowers/specs/2026-08-07-dashboard-redesign-design.md).

Not part of the assessment brief's scope -- see EXTENSIONS.md. Unlike
core.store.InMemoryBookingStore, this survives a server restart: it's a
JSON file (default pending_requests.json, gitignored), a dict keyed by
request id, rewritten in full on every mutation. Prototype-scale data (a
human review queue, not a high-volume log), so whole-file rewrite is
simple and sufficient -- no partial-write/append format needed.

Datetime handling: BookingRequest.fields sometimes holds a real datetime
under "start_time" (every current skill pack's convention -- see
core/audit.py's _json_safe docstring for the same assumption elsewhere
in this project). That value is serialized to an ISO 8601 string for the
JSON file and parsed back to a datetime on read, so a caller reconstructing
a BookingRequest from a stored item gets the same type slot_rules()
expects, not a string it would crash on.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union


class PendingRequestStore:
    """File-backed accept/reject queue -- see this module's docstring.

    Like `core.store.InMemoryBookingStore` (see `http_api.py`'s module
    docstring), `add`/`remove` are whole-file read-modify-write with no
    internal locking, so two truly concurrent mutations under the
    threaded dev server are not guaranteed to serialize correctly and one
    could be lost -- a known, documented limitation of this prototype,
    not something this class guards against with a lock.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def add(self, sector: str, text: str, fields: dict, skill_pack: str, reason: str) -> str:
        """Persist a new pending item, return its id."""
        items = self._read()
        request_id = uuid.uuid4().hex
        items[request_id] = {
            "id": request_id,
            "sector": sector,
            "text": text,
            "fields": self._serialize_fields(fields),
            "skill_pack": skill_pack,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(items)
        return request_id

    def list(self, sector: Optional[str] = None) -> List[dict]:
        items = [self._deserialize_item(item) for item in self._read().values()]
        if sector is not None:
            items = [item for item in items if item["sector"] == sector]
        return items

    def get(self, request_id: str) -> Optional[dict]:
        items = self._read()
        raw = items.get(request_id)
        return self._deserialize_item(raw) if raw is not None else None

    def remove(self, request_id: str) -> None:
        items = self._read()
        items.pop(request_id, None)
        self._write(items)

    # -- internals --------------------------------------------------------

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write(self, items: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    @staticmethod
    def _serialize_fields(fields: dict) -> dict:
        serialized = dict(fields)
        if isinstance(serialized.get("start_time"), datetime):
            serialized["start_time"] = serialized["start_time"].isoformat()
        return serialized

    @staticmethod
    def _deserialize_item(item: dict) -> dict:
        result = dict(item)
        fields = dict(result["fields"])
        if isinstance(fields.get("start_time"), str):
            fields["start_time"] = datetime.fromisoformat(fields["start_time"])
        result["fields"] = fields
        return result
