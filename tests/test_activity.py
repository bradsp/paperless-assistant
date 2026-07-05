"""ActivityStore tests (prompt 013): CRUD, filtered + paginated queries, purge,
thread-safe concurrent writes, and the field-level diff helpers.

Fully OFFLINE — SQLite under a tmp path, no Paperless, no network.
"""
from __future__ import annotations

import threading
import time

import pytest

from paperless_assistant.activity import (
    ActivityStore, diff_fields, tag_delta, paperless_doc_url, changes_summary,
)


def _store(tmp_path):
    return ActivityStore(str(tmp_path / "data" / "activity.db"))


def _entry(**over):
    e = {
        "run_id": "run-1", "doc_id": 1, "doc_title": "Invoice",
        "stage": "metadata", "dry_run": False, "status": "done",
        "changes": {"fields": {"title": {"before": "old", "after": "new"}}},
        "summary": "title='new'", "paperless_url": "http://p/documents/1/details",
    }
    e.update(over)
    return e


# ===========================================================================
# schema / record / get
# ===========================================================================
def test_record_and_get_roundtrip(tmp_path):
    st = _store(tmp_path)
    rid = st.record(_entry())
    assert isinstance(rid, int) and rid > 0
    row = st.get(rid)
    assert row["doc_id"] == 1
    assert row["doc_title"] == "Invoice"
    assert row["stage"] == "metadata"
    assert row["dry_run"] is False
    assert row["status"] == "done"
    assert row["changes"]["fields"]["title"]["after"] == "new"
    st.close()


def test_wal_mode_enabled(tmp_path):
    st = _store(tmp_path)
    with st._lock:
        mode = st._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    st.close()


def test_indexes_exist_on_ts_and_doc(tmp_path):
    st = _store(tmp_path)
    with st._lock:
        idx = {r[0] for r in st._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_activity_ts" in idx and "idx_activity_doc" in idx
    st.close()


# ===========================================================================
# query: filters + pagination + total
# ===========================================================================
def test_query_filters_and_total(tmp_path):
    st = _store(tmp_path)
    base = time.time()
    st.record(_entry(doc_id=1, stage="triage", status="wrote", dry_run=False, ts=base))
    st.record(_entry(doc_id=2, stage="metadata", status="done", dry_run=True, ts=base + 1))
    st.record(_entry(doc_id=2, stage="metadata", status="ERROR", dry_run=False, ts=base + 2))

    assert st.query(doc_id=2)["total"] == 2
    assert st.query(stage="triage")["total"] == 1
    assert st.query(dry_run=True)["total"] == 1
    assert st.query(status="ERROR")["total"] == 1
    assert st.query(since=base + 1.5)["total"] == 1        # only the newest
    assert st.query(until=base + 0.5)["total"] == 1        # only the oldest
    st.close()


def test_query_search_matches_title_and_changes(tmp_path):
    st = _store(tmp_path)
    st.record(_entry(doc_id=1, doc_title="Electric Bill",
                     changes={"fields": {"correspondent": {"before": None, "after": "PGE"}}},
                     summary="corr=PGE"))
    st.record(_entry(doc_id=2, doc_title="Water Bill", summary="title='x'"))
    assert st.query(search="Electric")["total"] == 1        # title match
    assert st.query(search="PGE")["total"] == 1             # changes-json match
    assert st.query(search="Bill")["total"] == 2            # both titles
    st.close()


def test_query_pagination_newest_first(tmp_path):
    st = _store(tmp_path)
    base = time.time()
    for i in range(10):
        st.record(_entry(doc_id=i, ts=base + i))
    page1 = st.query(limit=3, offset=0)
    assert page1["total"] == 10 and len(page1["rows"]) == 3
    # newest first: doc_id 9, 8, 7
    assert [r["doc_id"] for r in page1["rows"]] == [9, 8, 7]
    page2 = st.query(limit=3, offset=3)
    assert [r["doc_id"] for r in page2["rows"]] == [6, 5, 4]
    st.close()


def test_stats(tmp_path):
    st = _store(tmp_path)
    base = time.time()
    st.record(_entry(ts=base))
    st.record(_entry(ts=base + 100))
    s = st.stats()
    assert s["count"] == 2
    assert s["oldest_ts"] == base and s["newest_ts"] == base + 100
    assert s["size_bytes"] > 0
    st.close()


# ===========================================================================
# purge
# ===========================================================================
def test_purge_deletes_old_keeps_new(tmp_path):
    st = _store(tmp_path)
    now = time.time()
    st.record(_entry(doc_id=1, ts=now - 100 * 86400))   # old
    st.record(_entry(doc_id=2, ts=now - 1 * 86400))     # recent
    cutoff = now - 90 * 86400
    deleted = st.purge(cutoff)
    assert deleted == 1
    remaining = st.query()
    assert remaining["total"] == 1
    assert remaining["rows"][0]["doc_id"] == 2
    st.close()


# ===========================================================================
# thread-safe concurrent writes (WAL + lock)
# ===========================================================================
def test_concurrent_writes_are_safe(tmp_path):
    st = _store(tmp_path)
    n_threads, per_thread = 8, 50

    def worker(tid):
        for i in range(per_thread):
            st.record(_entry(doc_id=tid * 1000 + i, run_id=f"t{tid}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert st.query(limit=1)["total"] == n_threads * per_thread
    st.close()


# ===========================================================================
# diff helpers
# ===========================================================================
def test_diff_fields_only_changed():
    before = {"title": "a", "correspondent": "Acme", "ai_stage": "triaged"}
    after = {"title": "b", "correspondent": "Acme", "ai_stage": "metadata_done"}
    d = diff_fields(before, after)
    assert set(d) == {"title", "ai_stage"}        # correspondent unchanged -> omitted
    assert d["title"] == {"before": "a", "after": "b"}


def test_diff_fields_none_vs_empty_string_equal():
    # None and "" are treated as the same "no value" -> not a change.
    assert diff_fields({"x": None}, {"x": ""}) == {}
    assert diff_fields({"x": None}, {"x": "y"}) == {"x": {"before": None, "after": "y"}}


def test_tag_delta():
    d = tag_delta(["a", "b"], ["b", "c"])
    assert d == {"added": ["c"], "removed": ["a"]}
    assert tag_delta([], []) == {}


def test_paperless_doc_url():
    assert paperless_doc_url("http://p:8000/", 5) == "http://p:8000/documents/5/details"
    assert paperless_doc_url("", 5) == ""
    assert paperless_doc_url("http://p", None) == ""


def test_changes_summary():
    ch = {"fields": {"title": {"before": "a", "after": "b"}},
          "tags": {"added": ["billing"]}, "flags": ["ai-new-taxonomy"]}
    s = changes_summary(ch)
    assert "title=" in s and "+billing" in s and "[ai-new-taxonomy]" in s
