"""Unit tests for PaperlessClient seams: retry/backoff (I7), error surfacing
(I6), pagination, post_document, task polling. Uses the `responses` library to
mock the HTTP surface - no live Paperless."""
import time

import pytest
import responses

from paperless_assistant.client import PaperlessClient


BASE = "http://paperless.test:8000"


def _client():
    return PaperlessClient(BASE, "tok")


@responses.activate
def test_request_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # no real waiting
    url = f"{BASE}/api/custom_fields/"
    responses.add(responses.GET, url, status=429, headers={"Retry-After": "0"})
    responses.add(responses.GET, url, json={"results": [], "next": None}, status=200)
    r = _client().request("GET", url)
    assert r.status_code == 200
    assert len(responses.calls) == 2


@responses.activate
def test_request_retries_on_5xx(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    url = f"{BASE}/api/tags/"
    responses.add(responses.GET, url, status=503)
    responses.add(responses.GET, url, json={"ok": True}, status=200)
    assert _client().request("GET", url).json() == {"ok": True}


@responses.activate
def test_request_surfaces_server_error_message(monkeypatch):
    # I6: the server's real validation message must appear in the exception.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    url = f"{BASE}/api/documents/5/"
    responses.add(responses.PATCH, url, json={"custom_fields": ["Invalid option id"]}, status=400)
    with pytest.raises(Exception) as ei:
        _client().request("PATCH", url, json={})
    assert "Invalid option id" in str(ei.value)


@responses.activate
def test_iter_documents_paginates():
    p1 = f"{BASE}/api/documents/?fields=id&page_size=100"
    responses.add(responses.GET, f"{BASE}/api/documents/",
                  json={"results": [{"id": 1}, {"id": 2}], "next": f"{BASE}/api/documents/?page=2"})
    responses.add(responses.GET, f"{BASE}/api/documents/",
                  json={"results": [{"id": 3}], "next": None})
    ids = [d["id"] for d in _client().iter_documents("id")]
    assert ids == [1, 2, 3]


@responses.activate
def test_get_all_paginates():
    responses.add(responses.GET, f"{BASE}/api/tags/",
                  json={"results": [{"id": 1}], "next": f"{BASE}/api/tags/?page=2"})
    responses.add(responses.GET, f"{BASE}/api/tags/", json={"results": [{"id": 2}], "next": None})
    assert [t["id"] for t in _client().get_all("tags")] == [1, 2]


@responses.activate
def test_download_original_returns_bytes():
    responses.add(responses.GET, f"{BASE}/api/documents/9/download/", body=b"%PDF-1.4 fake")
    assert _client().download_original(9) == b"%PDF-1.4 fake"


@responses.activate
def test_post_document_returns_task_uuid():
    responses.add(responses.POST, f"{BASE}/api/documents/post_document/", json="task-abc-123", status=200)
    doc = {"title": "T", "correspondent": 5, "document_type": 8, "created": "2024-01-01", "tags": [1, 2]}
    assert _client().post_document(b"pdf", doc, "t.pdf") == "task-abc-123"


@responses.activate
def test_find_new_doc_by_task_success(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses.add(responses.GET, f"{BASE}/api/tasks/",
                  json=[{"status": "SUCCESS", "related_document": 321}])
    assert _client().find_new_doc_by_task("task-x", timeout=5) == 321


@responses.activate
def test_find_new_doc_by_task_failure_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses.add(responses.GET, f"{BASE}/api/tasks/", json=[{"status": "FAILURE"}])
    with pytest.raises(RuntimeError):
        _client().find_new_doc_by_task("task-x", timeout=5)


# ---------------------------------------------------------------------------
# Paperless v2 + v3 compatibility: version=9 Accept pin, auto-detection, and
# defensive tasks parsing across the v9 (bare list) and v10 (paginated) shapes.
# ---------------------------------------------------------------------------
class _RecordingLogger:
    """Minimal stand-in for obs.JsonLogger capturing emitted events."""

    def __init__(self):
        self.events = []

    def event(self, event, level="info", **fields):
        rec = {"event": event, "level": level, **fields}
        self.events.append(rec)
        return rec


@responses.activate
def test_accept_header_pins_api_version_9():
    # Requirement 1: every request must carry `Accept: application/json; version=9`
    # so v2 and v3 servers both serve the v9-shaped responses this client parses.
    url = f"{BASE}/api/ui_settings/"
    responses.add(responses.GET, url, json={"user": {}}, status=200)
    _client().request("GET", url)
    assert responses.calls[0].request.headers["Accept"] == "application/json; version=9"


@responses.activate
def test_detects_v2_from_x_api_version_9():
    url = f"{BASE}/api/ui_settings/"
    responses.add(responses.GET, url, json={"user": {}}, status=200,
                  headers={"X-Api-Version": "9", "X-Version": "2.20.15"})
    log = _RecordingLogger()
    c = PaperlessClient(BASE, "tok", logger=log)
    c.request("GET", url)
    assert c.api_version == 9
    assert c.is_v3 is False
    assert c.server_generation == "v2"
    assert c.server_version == "2.20.15"
    ev = [e for e in log.events if e["event"] == "paperless_version_detected"]
    assert len(ev) == 1
    assert ev[0]["detected"] is True
    assert ev[0]["generation"] == "v2"


@responses.activate
def test_detects_v3_from_x_api_version_10():
    url = f"{BASE}/api/ui_settings/"
    responses.add(responses.GET, url, json={"user": {}}, status=200,
                  headers={"X-Api-Version": "10", "X-Version": "3.0.0-beta"})
    log = _RecordingLogger()
    c = PaperlessClient(BASE, "tok", logger=log)
    c.request("GET", url)
    assert c.api_version == 10
    assert c.is_v3 is True
    assert c.server_generation == "v3"
    ev = [e for e in log.events if e["event"] == "paperless_version_detected"]
    assert len(ev) == 1
    assert ev[0]["detected"] is True
    assert ev[0]["generation"] == "v3"


@responses.activate
def test_missing_header_falls_back_to_v2_and_logs():
    # Requirement 2: absent/inconclusive header -> default to the v2/API-v9
    # known-good path (never fail closed, never silently assume v3) + log it once.
    url = f"{BASE}/api/ui_settings/"
    responses.add(responses.GET, url, json={"user": {}}, status=200)  # no headers
    log = _RecordingLogger()
    c = PaperlessClient(BASE, "tok", logger=log)
    c.request("GET", url)
    assert c.api_version == 9  # fallback
    assert c.server_generation == "v2"
    ev = [e for e in log.events if e["event"] == "paperless_version_detected"]
    assert len(ev) == 1
    assert ev[0]["detected"] is False
    assert ev[0]["generation"] == "v2"


@responses.activate
def test_version_detection_is_cached_not_recomputed(monkeypatch):
    # Detected once, then cached: a later header change does not re-detect.
    url = f"{BASE}/api/ui_settings/"
    responses.add(responses.GET, url, json={"user": {}}, status=200,
                  headers={"X-Api-Version": "9"})
    responses.add(responses.GET, url, json={"user": {}}, status=200,
                  headers={"X-Api-Version": "10"})
    log = _RecordingLogger()
    c = PaperlessClient(BASE, "tok", logger=log)
    c.request("GET", url)
    c.request("GET", url)
    assert c.api_version == 9  # first detection wins; never recomputed
    ev = [e for e in log.events if e["event"] == "paperless_version_detected"]
    assert len(ev) == 1  # logged exactly once


@responses.activate
def test_find_new_doc_by_task_v9_bare_list(monkeypatch):
    # v9 shape: bare list, `related_document` int (the pin restores this on v3).
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses.add(responses.GET, f"{BASE}/api/tasks/",
                  json=[{"status": "SUCCESS", "related_document": 321, "result": "321"}])
    assert _client().find_new_doc_by_task("task-x", timeout=5) == 321


@responses.activate
def test_find_new_doc_by_task_v10_paginated_renamed(monkeypatch):
    # v10 shape: paginated dict + `related_document_ids` list, no `related_document`.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses.add(
        responses.GET, f"{BASE}/api/tasks/",
        json={
            "count": 1, "next": None, "previous": None,
            "results": [{
                "status": "SUCCESS",
                "related_document_ids": [654],
                "result_data": {"document_id": 654},
            }],
        },
    )
    assert _client().find_new_doc_by_task("task-x", timeout=5) == 654


@responses.activate
def test_find_new_doc_by_task_v10_result_data_only(monkeypatch):
    # v10 fallback path: only result_data.document_id present.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses.add(
        responses.GET, f"{BASE}/api/tasks/",
        json={"results": [{"status": "SUCCESS", "result_data": {"document_id": 99}}]},
    )
    assert _client().find_new_doc_by_task("task-x", timeout=5) == 99


@responses.activate
def test_find_new_doc_by_task_v10_paginated_failure_raises(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    responses.add(responses.GET, f"{BASE}/api/tasks/",
                  json={"results": [{"status": "FAILURE"}]})
    with pytest.raises(RuntimeError):
        _client().find_new_doc_by_task("task-x", timeout=5)
