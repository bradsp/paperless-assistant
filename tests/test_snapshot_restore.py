"""I2/I4: snapshot -> mutate -> restore round-trip (reversibility).

Demonstrates that a snapshot taken before a write can be replayed to restore the
document's original state - the rollback guarantee the whole safety model rests
on. Uses a fake session so no live Paperless.
"""
import json

from paperless_assistant.client import PaperlessClient
from paperless_assistant.fields import CustomFieldResolver
from paperless_assistant.safety import SafetyLayer

from fakes import FakeSession, FakeResponse


CUSTOM_FIELDS = {
    "results": [
        {"id": 1, "name": "ocr_quality", "data_type": "float"},
        {"id": 2, "name": "ai_stage", "data_type": "select",
         "extra_data": {"select_options": [{"id": "opt-t", "label": "triaged"}]}},
        {"id": 3, "name": "ai_notes", "data_type": "text"},
    ],
    "next": None,
}


def test_snapshot_mutate_restore_roundtrip(tmp_path):
    session = FakeSession()
    session.add_json("GET", "/api/custom_fields/", CUSTOM_FIELDS)
    client = PaperlessClient("http://paperless.test:8000", "tok", session=session)
    resolver = CustomFieldResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=tmp_path)

    # Original document state (a human-curated doc).
    original = {
        "id": 50,
        "title": "Original Title",
        "correspondent": 5,
        "document_type": 8,
        "tags": [10, 20],
        "custom_fields": [{"field": 1, "value": 0.1}, {"field": 99, "value": "human"}],
    }

    # 1. Snapshot before any write.
    safety.snapshot(original)
    assert (tmp_path / "50.json").exists()

    # 2. Simulate a mutating write (title/tags clobbered on the server). We
    #    don't actually mutate `original`; the snapshot on disk holds the truth.
    captured = {}
    session.add("PATCH", "/api/documents/50/",
                lambda m, u, **kw: (captured.__setitem__("body", kw["json"]), FakeResponse(200, {}))[1])

    # 3. Restore replays the snapshot back onto the document.
    body = safety.restore(50)

    # The restore PATCH carries the ORIGINAL values, reversing any mutation.
    assert body["title"] == "Original Title"
    assert body["correspondent"] == 5
    assert body["document_type"] == 8
    assert body["tags"] == [10, 20]
    cf = {c["field"]: c["value"] for c in body["custom_fields"]}
    assert cf[1] == 0.1
    assert cf[99] == "human"
    # And it was actually sent to the server.
    assert captured["body"]["title"] == "Original Title"


def test_restore_missing_snapshot_raises(tmp_path):
    session = FakeSession()
    session.add_json("GET", "/api/custom_fields/", CUSTOM_FIELDS)
    client = PaperlessClient("http://paperless.test:8000", "tok", session=session)
    resolver = CustomFieldResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=tmp_path)
    import pytest

    with pytest.raises(FileNotFoundError):
        safety.restore(999)
