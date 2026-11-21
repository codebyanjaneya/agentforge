"""M1-F: GET /history pagination, cursor integrity and ownership predicates."""

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta

import pytest

from memory.cursor import CursorError, decode_cursor, encode_cursor
from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)

BASE = datetime(2026, 3, 1, 12, 0, 0)


def seed(api, uid, wid, n, summary="m", step_seconds=1, group=1):
    """n rows; ``group`` consecutive rows share a timestamp (so ``id`` must break ties)."""
    for i in range(n):
        api.add_memory(uid, wid, f"{summary}{i}", BASE + timedelta(seconds=(i // group) * step_seconds))


def get(api, params=None, sub="user-a", email="a@example.com"):
    return api.client.get("/history", params=params, headers=api.auth(sub, email))


def walk(api, limit, **kw):
    pages, cursor = [], None
    while True:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        body = get(api, params, **kw).json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        assert len(pages) <= 500, "pagination does not terminate"  # a cursor that never advances must fail, not hang
        if cursor is None:
            return pages


def test_default_page_size_is_50_and_maximum_is_100(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 130)
    assert len(get(api).json()["items"]) == 50
    assert len(get(api, {"limit": 100}).json()["items"]) == 100
    assert len(get(api, {"limit": 1}).json()["items"]) == 1
    for bad in (0, -1, 101, 1000, "x", ""):
        assert get(api, {"limit": bad}).status_code == 422, bad


def test_ordering_is_newest_first_with_id_as_tie_break(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 20, group=4)  # five distinct timestamps, four rows each
    items = get(api, {"limit": 100}).json()["items"]
    keys = [(i["created_at"], i["id"]) for i in items]
    assert keys == sorted(keys, reverse=True) and len(set(keys)) == 20


@pytest.mark.parametrize("limit,group", [(7, 1), (7, 4), (1, 3), (20, 20), (100, 5)])
def test_cursor_pagination_has_no_duplicates_or_omissions(api, limit, group):
    uid, wid = api.provision()
    seed(api, uid, wid, 53, group=group)
    pages = walk(api, limit)
    flat = [i["id"] for page in pages for i in page]
    everything = [i["id"] for i in get(api, {"limit": 100}).json()["items"]] if limit != 100 else None
    assert len(flat) == len(set(flat)) == 53
    all_rows = api.db.rows("SELECT id FROM agent_memories ORDER BY created_at DESC, id DESC")
    assert flat == [r[0] for r in all_rows]
    assert all(len(p) == limit for p in pages[:-1]) and 0 < len(pages[-1]) <= limit
    if everything is not None:
        assert flat[:100] == everything[:len(flat)]


def test_exact_multiple_and_empty_history(api):
    uid, wid = api.provision()
    assert get(api).json() == {"items": [], "next_cursor": None}
    seed(api, uid, wid, 10)
    pages = walk(api, 5)
    assert [len(p) for p in pages] == [5, 5]  # no phantom empty trailing page


def test_new_rows_between_pages_do_not_cause_duplicates(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 10)
    first = get(api, {"limit": 4}).json()
    api.add_memory(uid, wid, "newest", BASE + timedelta(days=1))
    rest = []
    cursor = first["next_cursor"]
    while cursor:
        body = get(api, {"limit": 4, "cursor": cursor}).json()
        rest += body["items"]
        cursor = body["next_cursor"]
    ids = [i["id"] for i in first["items"] + rest]
    assert len(ids) == len(set(ids)) == 10 and "newest" not in [i["summary"] for i in first["items"] + rest]


def test_malformed_and_tampered_cursors_are_rejected(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 10)
    good = get(api, {"limit": 3}).json()["next_cursor"]
    version, body, check = good.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))

    def forge(data, checksum=None):
        raw = json.dumps(data, separators=(",", ":")).encode()
        b = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"v1.{b}.{checksum or check}"

    bad = [
        "garbage", "a.b", "a.b.c.d", f"v2.{body}.{check}", f"v1.{body}.00000000", f"v1.{body}x.{check}", f"v1.{body}.",
        forge({**payload, "i": payload["i"] + 1}),          # edited payload, old checksum
        forge({"t": payload["t"], "i": "7"}), forge({"t": payload["t"]}), forge({"t": "not-a-date", "i": 1}),
        "v1." + "A" * 500 + "." + check, "%00", "....",
    ]
    for cursor in bad:
        r = get(api, {"cursor": cursor})
        assert r.status_code in (400, 422), cursor
        assert r.status_code == 400 and r.json() == {"detail": "Invalid cursor"} or len(cursor) > 300
    assert get(api, {"cursor": good}).status_code == 200


def _signed_cursor(timestamp, row_id=5):
    raw = json.dumps({"t": timestamp, "i": row_id}, separators=(",", ":")).encode()
    return "v1." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + hashlib.sha256(raw).hexdigest()[:8]


def test_cursor_accepts_only_naive_timestamps_even_with_a_valid_checksum(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 3)
    assert get(api, {"cursor": _signed_cursor("2026-03-01T12:00:00")}).status_code == 200  # same payload, naive: accepted
    for aware in ("2026-03-01T12:00:00+00:00", "2026-03-01T12:00:00+05:30", "2026-03-01T12:00:00Z"):
        r = get(api, {"cursor": _signed_cursor(aware)})
        assert r.status_code == 400 and r.json() == {"detail": "Invalid cursor"}, aware
    with pytest.raises(CursorError):
        decode_cursor(_signed_cursor("2026-03-01T12:00:00+00:00"))
    assert decode_cursor(_signed_cursor("2026-03-01T12:00:00")) == (datetime(2026, 3, 1, 12, 0, 0), 5)


def test_cursor_unit_roundtrip_and_strictness():
    now = datetime(2026, 1, 2, 3, 4, 5, 123456)
    assert decode_cursor(encode_cursor(now, 42)) == (now, 42)
    assert decode_cursor(encode_cursor(datetime(2026, 1, 2), 1)) == (datetime(2026, 1, 2), 1)
    for bad in (None, 5, "", "v1", "v1.x.y", object()):
        with pytest.raises(CursorError):
            decode_cursor(bad)
    with pytest.raises(CursorError):
        decode_cursor(encode_cursor(now, 0))  # id must be positive
    assert "@" not in encode_cursor(now, 1) and encode_cursor(now, 1).startswith("v1.")


def test_a_cursor_never_widens_access(api):
    uid_a, ws_a = api.provision("user-a", "a@example.com")
    uid_b, ws_b = api.provision("user-b", "b@example.com")
    seed(api, uid_a, ws_a, 8, summary="A")
    seed(api, uid_b, ws_b, 8, summary="B")
    cursor_a = get(api, {"limit": 3}).json()["next_cursor"]
    r = get(api, {"limit": 100, "cursor": cursor_a}, sub="user-b", email="b@example.com")  # B replays A's cursor
    assert r.status_code == 200 and {i["summary"][0] for i in r.json()["items"]} == {"B"}


def test_legacy_null_owned_rows_are_invisible(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 3, summary="owned")
    with api.db.session_factory() as s, s.begin():
        from memory.models import AgentMemory
        for kw in ({"user_email": "a@example.com"}, {"user_email": None}, {"user_email": "voice-call@agentforge.local"},
                   {"user_email": "a@example.com", "user_id": uid}, {"user_email": "a@example.com", "workspace_id": wid}):
            s.add(AgentMemory(agent_name="ceo", summary="LEGACY", created_at=BASE + timedelta(days=5), **kw))
    summaries = [i["summary"] for page in walk(api, 2) for i in page]
    assert sorted(summaries) == ["owned0", "owned1", "owned2"]


def test_other_workspace_rows_are_invisible(api):
    uid, wid = api.provision()
    other_ws = uuid.uuid4()
    with api.db.session_factory() as s, s.begin():
        from memory.models import Workspace, WorkspaceMembership
        s.add(Workspace(id=other_ws, name="Team", is_personal=False))
        s.flush()
        s.add(WorkspaceMembership(workspace_id=other_ws, user_id=uid, role="owner"))
    seed(api, uid, wid, 2, summary="personal")
    seed(api, uid, other_ws, 2, summary="team")
    assert sorted(i["summary"] for i in get(api, {"limit": 100}).json()["items"]) == ["personal0", "personal1"]


def test_history_requires_authentication_and_is_never_unscoped(api):
    uid, wid = api.provision()
    seed(api, uid, wid, 2)
    assert api.client.get("/history").status_code == 401
    assert api.client.get("/history", params={"user_email": "a@example.com"}).status_code == 401
    assert api.client.get("/history/a@example.com", headers=api.auth()).status_code == 404
