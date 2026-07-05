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
