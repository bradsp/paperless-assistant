# Paperless-NGX v2 → v3 (beta) REST API Migration Analysis

**Scope:** the REST surfaces `paperless-assistant` actually uses (client.py, fields.py,
metadata.py, taxonomy.py, webhook.py, doctor.py). Deliverable is analysis only — no source
was modified.

**Research date:** 2026-07-21. **Sources pinned to:** `v2.20.15` (latest stable) and
`v3.0.0-beta.rc1` (first v3 beta; `rc2` also released). All version-config claims below are
confirmed against upstream **source**, not memory.

---

## Summary (verdict)

v3 is **real** (`v3.0.0-beta.rc1`, first published 2026-05-05; `rc2` followed). Latest **stable**
remains `v2.20.15`. For this repo, v3 introduces exactly **one breaking change that matters** — and
it is not an endpoint move or a field rename we call directly. It is a **default API-version bump**:
v2.20.15 serves API **version 9** to a client that sends no `Accept: ...; version=` parameter; v3
serves API **version 10** to that same client. Our HTTP client sends a bare `Accept: application/json`
(client.py:47), so on v3 it silently gets v10-shaped responses. The **only** surface whose v10 shape
differs from what our code parses is **`/api/tasks/`** (post-and-poll after `post_document`), which
breaks two ways under v10: the response becomes **paginated** (a dict, not a bare list) and the task
fields are **renamed** (`related_document`/`result` → `related_document_ids`/`result_data`).

**Simultaneous v2 + v3 support: YES, and cheaply — Conditional on one change.** Pin the request API
version by sending `Accept: application/json; version=9` on every call. Version 9 is in
`ALLOWED_VERSIONS` on **both** v2.20.15 (`["1".."9"]`) and v3 (`["9","10"]`), and v3 preserves the
full v9 task shape (bare list + old field names) via a dedicated backwards-compat serializer. That
single header change makes every surface this repo uses behave **identically** on v2 and v3, with no
version branching. (Belt-and-suspenders: also teach `find_new_doc_by_task` to accept the v10 shape,
because v9 is only guaranteed for ~1 year after v10's release.) Every other surface we use — documents
list/detail, `download/`, `post_document/`, custom fields, taxonomy lists, the webhook `{doc_url}`
nudge, and doctor's `/api/ui_settings/` probe — is **unchanged** between the v9 and v10 shapes.

---

## Version detection mechanism (verified, high confidence)

Both v2 and v3 advertise their API level the **same way**: on **any authenticated** request the
`ApiVersionMiddleware` stamps two response headers.

- **`X-Api-Version`** — the server's highest supported API version = `ALLOWED_VERSIONS[-1]`.
  - v2.20.15 → **`9`**
  - v3.0.0-beta → **`10`**
- **`X-Version`** — the server's release string (`__full_version_str__`), e.g. `2.20.15` or the
  built beta's version string.

Source (v3 middleware): `response["X-Api-Version"] = versions[len(versions) - 1]` and
`response["X-Version"] = version.__full_version_str__`, set only `if request.user.is_authenticated`
(`src/paperless/middleware.py`, v3.0.0-beta.rc1). The published API doc states the same detection
procedure: "Perform an authenticated request … the response will include `X-Api-Version: 10` /
`X-Version: <server-version>`" (`docs/api.md`, v3.0.0-beta.rc1, "API Versioning").

**Config constants (source-confirmed):**

| | v2.20.15 | v3.0.0-beta.rc1 |
|---|---|---|
| `DEFAULT_VERSION` (served when request omits `version=`) | `"9"` | `"10"` |
| `ALLOWED_VERSIONS` | `["1","2","3","4","5","6","7","8","9"]` | `["9","10"]` |
| versioning class | `AcceptHeaderVersioning` | `AcceptHeaderVersioning` |
| invalid/too-old `version=` | `406 Not Acceptable` | `406 Not Acceptable` |

v2: `src/paperless/settings.py` L350-354. v3: `src/paperless/settings/__init__.py` L157-161.

**Practical detection to code against:** issue one authenticated GET (we already hit
`/api/ui_settings/` in doctor.py:117) and read the integer `X-Api-Version` header. `>=10` ⇒ v3-line
server, `==9` ⇒ v2.20.x-line server. This is the concrete, reliable signal. (Note: our client's
`request()` returns the `requests.Response`, so `r.headers["X-Api-Version"]` is already available at
every call site — no extra round-trip needed.) **Caveat (low risk):** the header is only set for
authenticated requests, and `X-Version` on the `rc1` *tag* source still reads `2.20.15`
(`version.py` bump happens at final tag) — so trust `X-Api-Version` (which is derived from
`ALLOWED_VERSIONS` and *is* correct on the beta) over `X-Version` for branching.

---

## Per-surface change table

`X-Api-Version` served to a **version-less** client is **9 on v2, 10 on v3** — so "v3 shape" below
means the **v10** shape our client receives today if we do nothing. "Pin v9" = send
`Accept: application/json; version=9`.

| # | Surface (our call) | v2 shape (served: v9) | v3 shape (served: v10, if unpinned) | Change | Our file:line | Severity | Citation |
|---|---|---|---|---|---|---|---|
| 1 | Auth header `Authorization: Token <token>` | valid | valid (unchanged; Token is auth method #3) | none | client.py:47 | none | api.md "Authorization" |
| 2 | `GET /api/documents/?fields=&page_size=` (list) | `{count,next,results,…}` | same; `created` is a **date** (v9+, already true on v2); v10 adds `all` deprecation + doc-versions, none used | none | client.py:78-88 | none | api.md "API Changelog" v9/v10 |
| 3 | `GET /api/documents/{id}/` (detail) | doc dict | same root dict; `content` now resolves to **latest version** by default (`?version=` optional, we omit) | none (behavioral, benign) | client.py:90-102 | none | api.md "Document Versions" |
| 4 | `GET /api/documents/{id}/download/?original=true` | file bytes | same; now also accepts optional `?version=` | none | client.py:117-124 | none | api.md "Document Versions" |
| 5 | `POST /api/documents/post_document/` | HTTP 200, body = consume **task UUID** (string) | **unchanged** — still returns the UUID string | none | client.py:126-153 | none | api.md "File Uploads" |
| 6 | **`GET /api/tasks/?task_id=<uuid>`** (poll) | **bare list**; task has `status` (SUCCESS/…), `result` (string), `related_document` (int) | **paginated dict** `{count,next,results,all}`; task has `status`, **`result_data`** (obj), **`related_document_ids`** (list) — **no `result`, no `related_document`** | **reshaped** | client.py:155-175 (esp. 164-171) | **breaking** | serialisers.py TaskSerializerV9 vs V10; views.py TasksViewSet L4104-4141 |
| 7 | `GET /api/custom_fields/?page_size=200` | select `extra_data.select_options` = array of `{id,label}`; value written as option **id** | **identical** (this is the v7 format, present at both v9 and v10) | none | fields.py:56-61 | none | api.md v7 changelog |
| 8 | `GET /api/{tags,correspondents,document_types}/` + `?name__iexact=` | `{results:[…]}`, tag has `color`/`text_color` | **identical** | none | taxonomy.py:34-40,82-89; doctor.py:218 | none | api.md v2 changelog (Tag.color) |
| 9 | `GET /api/storage_paths/` (scope-listed; used indirectly) | `{results:[…]}` | **identical** (StoragePath viewset unchanged in shape) | none | — (client.get_all pattern) | none | views.py StoragePathViewSet L3793-3800 |
| 10 | `POST` create tag/correspondent/type | `{id,…}` | **identical** | none | taxonomy.py:63-89 | none | serialisers.py (unchanged) |
| 11 | `PATCH /api/documents/{id}/` (metadata write) | accepts `title,correspondent,document_type,tags,custom_fields` | **identical** for these fields (v10 doc-versions only affect *content* writes via `?version=`, which we never send) | none | metadata.py:219-234 | none | api.md "Document Versions" |
| 12 | `GET /api/ui_settings/` (connectivity + admin probe) | 200 w/ `user.is_superuser/is_staff` | **unchanged** for our fields (v3 format changes are additive) | none | doctor.py:92,117 | none | api.md v3 changelog |
| 13 | Webhook `{doc_url}` nudge payload (`…/documents/<id>/`) | id-extractable | **unchanged** — `doc_url = {PAPERLESS_URL}{BASE_URL}documents/{pk}/` | none | webhook.py:57,60-85 | none | workflows/actions.py L39 |

---

## The one breaking change, in detail (surface #6)

`find_new_doc_by_task` (client.py:155-175) does, per poll:

```python
r = self.request("GET", f"{self.base}/api/tasks/?task_id={task_uuid}")
results = r.json()
if results:
    task = results[0] if isinstance(results, list) else results   # (A)
    status = task.get("status")
    if status == "SUCCESS":
        doc_id = task.get("related_document") or task.get("result")  # (B)
```

Against v3 served at **v10** (the default for our version-less client):

- **(A) pagination:** v10 `TasksViewSet.paginate_queryset` returns a **paginated dict**
  (`{count,next,previous,results,all}`), not a bare list. `if results:` is truthy on the dict;
  `isinstance(results, list)` is False → `task = results` (the whole envelope) →
  `task.get("status")` is `None` → never SUCCESS → loop runs until `TimeoutError`.
  (v9 preserves the **non-paginated bare list**: `paginate_queryset` returns `None` when
  `request.version < 10`.)
- **(B) field renames:** even reaching the task object, v10 has **no** `related_document` and
  **no** `result` — they are `related_document_ids` (list) and `result_data` (object). Both
  `.get()`s return `None` → `doc_id = None`.

Source: `src/documents/views.py` `TasksViewSet` — `get_serializer_class` returns `TaskSerializerV9`
when `int(request.version) < 10` else `TaskSerializerV10` (L4104-4108); `paginate_queryset` returns
`None` for v9 (L4110-4114); the `task_id` filter is applied **for both versions** (L4137-4140).
`TaskSerializerV9` (serialisers.py L2487-2585) exposes `status` (uppercased Celery states:
`PENDING/STARTED/SUCCESS/FAILURE/REVOKED`), `result` (string, reconstructed from `result_data`), and
`related_document` (`= related_document_ids[0]`). `TaskSerializerV10` (L2442-2484) exposes
`related_document_ids`, `result_data`, `result_message`, `acknowledged`, etc.

**`?task_id=` filtering is NOT removed** — it works on both v9 and v10. Our status strings
(`SUCCESS`, `FAILURE`, `REVOKED`) are all preserved in v9. So the break is purely "we get the v10
shape when we didn't ask for a version."

---

## Compatibility plan sketch

**Primary fix (version-agnostic, one line — recommended):** add the API version to the client's
default headers so *every* request is pinned:

- `Accept: application/json; version=9` (client.py:46-48).
- Works on v2.20.15 (9 ∈ `["1".."9"]`) and v3 (9 ∈ `["9","10"]`).
- Restores the exact v9 task shape our code already parses (bare list, `related_document`, `result`,
  uppercase `status`) on v3, and changes **nothing** on v2 (v2's default is already 9). No branching.
- Zero effect on all other surfaces (they are identical at v9/v10 for what we read/write).

**Belt-and-suspenders (recommended alongside the pin):** make `find_new_doc_by_task` tolerate the
v10 shape too, so the repo survives the day v3 drops v9 (guaranteed only ~1 year after v10 ships):

- Unwrap pagination: `results = data["results"] if isinstance(data, dict) else data`.
- Extract id defensively:
  `doc_id = task.get("related_document") or (task.get("related_document_ids") or [None])[0] or (task.get("result_data") or {}).get("document_id") or task.get("result")`.
- Keep the existing `status in ("FAILURE","REVOKED")` check (both v9 and v10 use these).

**Optional (only if the pin is ever undesirable):** feature-detect via `X-Api-Version` (already on
every response, see detection section) and branch the two task shapes. This is strictly more code
than pinning + defensive parse; prefer the pin.

**Doctor enhancement (nice-to-have, not required):** surface the detected `X-Api-Version`/`X-Version`
in the doctor connectivity check so users see which API level they're on and whether the pin is
being honored. `doctor.py:117` already makes the authenticated call that returns the headers.

---

## Research questions — explicit answers

1. **Version signaling.** Authenticated responses carry `X-Api-Version` (= highest supported API
   version) and `X-Version` (= server release). v2 → `X-Api-Version: 9`; v3 → `X-Api-Version: 10`.
   Also negotiable via request `Accept: application/json; version=N`; server default is 9 (v2) / 10
   (v3). **Verdict:** mechanism unchanged; the *value* moved 9 → 10. (middleware.py; api.md; both
   settings files.)
2. **Endpoint paths.** None of the paths we call moved or were removed: `post_document/`,
   `download/`, `tasks/` (+ `?task_id=`), and taxonomy list endpoints all exist unchanged. (Note:
   the *task acknowledge* action was moved under `/api/tasks/acknowledge/` back in **API v6** —
   a v2-era change — and we don't use it.) **Verdict:** no path change affecting us.
3. **Request/response shapes.** Only `/api/tasks/` differs in a way we touch (v10 pagination +
   `related_document`→`related_document_ids`, `result`→`result_data`). Custom-field select objects
   (`{id,label}`, value = option id) are the v7 format — already in effect on v2, already handled by
   fields.py. `created` is a date (v9, already on v2). Notes `user` object (v8) — unused.
   **Verdict:** breaking only for tasks; everything else no-change.
4. **Pagination & query params.** `page_size`, `fields=`, and `next`-based paging are unchanged for
   documents and taxonomy lists. The one pagination *gotcha* is the **tasks** endpoint becoming
   paginated at v10 (bare list at v9). The `all` list param is deprecated at v10 — we don't use it.
   **Verdict:** no truncation risk on the lists we read; tasks pagination handled by pinning v9.
5. **Authentication.** `Authorization: Token <token>` is still fully supported in v3 (auth method #3
   of five). No JWT/per-scope requirement. **Caveat:** v3 makes `PAPERLESS_SECRET_KEY` mandatory; if
   an admin rotates it on upgrade, existing tokens are invalidated and must be reissued — an
   operational note, not an API-scheme change. **Verdict:** unchanged.
6. **Task polling.** `?task_id=` still works; `SUCCESS/FAILURE/REVOKED` statuses preserved. The
   object/list **shape** changed at v10 (see #3). **Verdict:** breaking at v10, fully mitigated by
   pinning v9 (or handling both shapes).
7. **Webhook / consumption.** The Workflow→Webhook `{doc_url}` placeholder still renders as
   `…/documents/<id>/`, which webhook.py's `/documents/(\d+)` regex matches. **Verdict:** no change;
   id-extraction unaffected.
8. **Deprecations & removals.** Nothing we depend on is removed in v3. Deprecated-but-present items
   we could *optionally* care about later: API **v9** itself (supported ≥1 year post-v10 then may be
   dropped) and the `all` list param (we don't use it). **Verdict:** no hard break beyond #6.
9. **Breaking-change summary (ranked).**
   1. **[BREAKING] `/api/tasks/` v10 shape** — default version bump 9→10 changes tasks to
      paginated + renamed fields; breaks `find_new_doc_by_task` (post-upload doc discovery →
      `TimeoutError`). Fix: pin `version=9` (+ defensive parse).
   2. *(operational, not API)* `PAPERLESS_SECRET_KEY` now required — may invalidate the API token on
      upgrade; user must reissue. Doctor already FAILs clearly on a rejected token (doctor.py:123-130).
   3. Everything else: **no change** for surfaces we use.

---

## Open questions / unverifiable items (guard defensively)

- **Beta is in flux.** Facts are pinned to `v3.0.0-beta.rc1` source. `rc2` and the final `3.0.0`
  could adjust `DEFAULT_VERSION`/`ALLOWED_VERSIONS` again. **Guard:** don't assume the default is 10;
  read `X-Api-Version` and always send an explicit `version=9` so the served version is deterministic
  regardless of the server's default.
- **`X-Version` value on real beta builds.** The `rc1` *tag* still has `version.py = (2,20,15)`, so
  the header may under-report on tag-built sources; released beta images set it correctly. **Guard:**
  branch on `X-Api-Version` (numeric, reliable), not on parsing `X-Version`.
- **v9 longevity on v3.** api.md's deprecation policy: older API versions supported "for at least one
  year" after a new version, "after that … may be dropped." So a pin to `version=9` is safe now but
  not forever. **Guard:** add the v10-shape handling to `find_new_doc_by_task` now, so a future v9
  removal is a no-op for us.
- **Exact v10 `result_data` keys for a *successful consume*.** The v9 serializer reconstructs from
  `result_data.get("document_id")` (serialisers.py L2543), which strongly implies the v10 success
  payload is `result_data = {"document_id": <id>, …}` and `related_document_ids = [<id>]`. Confirmed
  by code, but not exercised against a live beta here. **Guard:** the defensive extractor above tries
  `related_document_ids[0]`, then `result_data["document_id"]`.
- **storage_paths** is in scope but the repo has no *direct* call site in the files reviewed (it's
  read via the generic `client.get_all` pattern and listed in doctor's scope text). Its list shape is
  unchanged; no action needed.

---

## Sources (URL → claim it supports)

- https://github.com/paperless-ngx/paperless-ngx/releases/tag/v3.0.0-beta.rc1 — v3 beta exists;
  breaking-changes list incl. "Remove API v1 compatibility" (#12166), "drop support for api versions
  < 9" (#12284), "Redesign the task system" (#12584), "Task History Cleared on Upgrade".
- https://github.com/paperless-ngx/paperless-ngx/pull/12713 — `[Beta] Paperless-ngx v3.0.0 Beta`
  umbrella PR.
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/docs/api.md — API Versioning:
  default version **10**, allowed **9 and 10**, `Accept: application/json; version=10`, invalid ⇒
  406, detection via `X-Api-Version`/`X-Version`; Authorization (Token still supported);
  `post_document` returns the consume task UUID and `/api/tasks/?task_id={uuid}` is the documented
  poll; API changelog v6 (acknowledge moved), v7 (select fields → `{id,label}`, value = option id),
  v8 (notes user object), v9 (`created` = date), v10 (saved-view fields removed, individual edit
  endpoints, `all` deprecated).
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/docs/migration-v3.md —
  `PAPERLESS_SECRET_KEY` now required (may invalidate tokens/sessions); task history cleared on
  upgrade; no REST-path changes for our surfaces.
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/src/paperless/settings/__init__.py
  (L157-161) — v3 `DEFAULT_VERSION="10"`, `ALLOWED_VERSIONS=["9","10"]`, `AcceptHeaderVersioning`.
- https://github.com/paperless-ngx/paperless-ngx/blob/v2.20.15/src/paperless/settings.py (L350-354) —
  v2 `DEFAULT_VERSION="9"`, `ALLOWED_VERSIONS=["1".."9"]`.
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/src/paperless/middleware.py —
  `ApiVersionMiddleware` sets `X-Api-Version = ALLOWED_VERSIONS[-1]` and `X-Version = full version`,
  only for authenticated requests.
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/src/documents/serialisers.py
  (TaskSerializerV9 L2487-2585, TaskSerializerV10 L2442-2484) — v9 exposes `status`/`result`/
  `related_document`; v10 exposes `related_document_ids`/`result_data`/`result_message`; v9 success
  string derived from `result_data["document_id"]`.
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/src/documents/views.py
  (TasksViewSet L4055-4154) — `get_serializer_class`/`paginate_queryset` branch on
  `request.version < 10` (v9 = bare list, v10 = paginated); `task_id` filter applied for both;
  acknowledge action under the tasks viewset.
- https://github.com/paperless-ngx/paperless-ngx/blob/v3.0.0-beta.rc1/src/documents/workflows/actions.py
  (L39) — webhook context `doc_url = {PAPERLESS_URL}{BASE_URL}documents/{pk}/` (nudge id-extraction
  unaffected).
- https://github.com/paperless-ngx/paperless-ngx/pull/12584 — task system redesign; v9 response shape
  + `type` filter preserved for backward compatibility; new model fields
  (`related_document_ids`, `result_data`, `acknowledged`, …).
- https://github.com/paperless-ngx/paperless-ngx/releases — release index: latest stable `v2.20.15`
  (2026-04-27), beta line `v3.0.0-beta.rc1`/`rc2`.
