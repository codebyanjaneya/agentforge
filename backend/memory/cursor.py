"""Opaque history cursor over ``(created_at, id)`` (D027).

Format: ``v1.<base64url(json)>.<checksum>``; the JSON holds the last returned row's timestamp and id. The checksum
(first 8 hex of SHA-256 over the payload) detects corruption and casual tampering, it is NOT a secret and not
authorization. Isolation never depends on the cursor, because every history query is also constrained by the
caller's ``MemoryScope``; a forged but well-formed cursor can only choose another position within the caller's own
rows.
"""

import base64
import hashlib
import json
from datetime import datetime


class CursorError(ValueError):
    pass


def _check(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:8]


def encode_cursor(created_at: datetime, row_id: int) -> str:
    payload = json.dumps({"t": created_at.isoformat(), "i": int(row_id)}, separators=(",", ":")).encode()
    return f"v1.{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{_check(payload)}"


def decode_cursor(value) -> tuple[datetime, int]:
    try:
        version, body, check = value.split(".")
        if version != "v1" or len(body) > 200:
            raise ValueError
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if check != _check(payload):
            raise ValueError
        data = json.loads(payload)
        if not isinstance(data, dict) or set(data) != {"t", "i"} or type(data["i"]) is not int or data["i"] < 1:
            raise ValueError
        created_at = datetime.fromisoformat(data["t"])
        if created_at.tzinfo is not None:
            raise ValueError
        return created_at, data["i"]
    except Exception:  # noqa: BLE001 - any malformed cursor is one fixed error
        raise CursorError("invalid_cursor") from None
