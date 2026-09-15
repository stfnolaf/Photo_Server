import base64
import json
from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from photo_server.browsing import BrowseQuery, UserStatePatch, camera_time


def test_camera_date_keeps_local_calendar_and_rejects_invalid_exif():
    assert camera_time("2024-05-01T00:10:00+13:00") == datetime(2024, 5, 1, 0, 10)
    assert camera_time("2024-05-01T00:10:00") == datetime(2024, 5, 1, 0, 10)
    assert camera_time("0000:00:00 00:00:00") is None
    assert camera_time(None) is None


def test_cursor_roundtrip_binds_filters_but_not_page_size():
    query = BrowseQuery(q=" Sony ", favorite=True, date_to="2025-01-01")
    row = {"timeline_at": datetime(2024, 5, 1, 12, 0), "id": str(uuid4())}
    cursor = query.encode_cursor(row)
    assert query.model_copy(update={"cursor": cursor, "limit": 1}).decode_cursor() == (
        row["timeline_at"],
        row["id"],
    )
    with pytest.raises(ValueError, match="different filters"):
        BrowseQuery(cursor=cursor).decode_cursor()


@pytest.mark.parametrize("payload", [None, {}, [], [1, {}, [], 123], [1, "hash", "time", {}]])
def test_malformed_cursor_payload_is_rejected(payload):
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ValueError, match="Invalid cursor"):
        BrowseQuery(cursor=cursor).decode_cursor()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rating": None},
        {"rating": 6},
        {"rating": True},
        {"rating": "3"},
        {"favorite": 1},
        {"favorite": None},
        {"caption": "future"},
    ],
)
def test_user_state_rejects_ambiguous_or_out_of_scope_mutations(payload):
    with pytest.raises(ValidationError):
        UserStatePatch.model_validate(payload)


def test_state_patch_only_sets_supplied_fields():
    assert UserStatePatch(rating=0).model_dump(exclude_unset=True) == {"rating": 0}
    assert UserStatePatch(favorite=False).model_dump(exclude_unset=True) == {"favorite": False}


def test_reversed_date_range_is_rejected():
    with pytest.raises(ValidationError, match="Start date"):
        BrowseQuery(date_from="2025-01-02", date_to="2025-01-01")
