"""Offline fakes: a fake requests.Session for PaperlessClient and a stub
Anthropic client. Tests run with NO live Paperless and NO real Anthropic key.
"""
from __future__ import annotations

import json


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = text
        self.headers = {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Programmable fake session. `routes` maps (METHOD, url_substring) ->
    handler(method, url, **kw) returning a FakeResponse. `calls` records every
    request for assertions.
    """

    def __init__(self):
        self.headers = {}
        self.routes = []  # list of (method, substring, handler)
        self.calls = []

    def add(self, method, substring, handler):
        self.routes.append((method.upper(), substring, handler))

    def add_json(self, method, substring, json_data, status_code=200):
        self.add(method, substring, lambda m, u, **kw: FakeResponse(status_code, json_data))

    def request(self, method, url, **kw):
        self.calls.append((method.upper(), url, kw))
        for m, sub, handler in self.routes:
            if m == method.upper() and sub in url:
                return handler(method, url, **kw)
        raise AssertionError(f"no fake route for {method} {url}")

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# ===========================================================================
# Stateful fake Paperless for onboarding/sweep tests (setup/doctor/run). Models
# custom_fields, tags, and documents in-memory over the PaperlessClient's
# request()/get_all() surface. NO live server.
# ===========================================================================
class FakePaperless:
    """A stateful fake wired into a PaperlessClient via its `session`.

    Supports: GET/POST /api/custom_fields/, GET/POST /api/tags/ (incl.
    ?name__iexact=), GET/PATCH /api/documents/, GET /api/ (root), and
    /api/ui_settings/ (for the doctor admin-token probe).
    """

    def __init__(self, *, fields=None, tags=None, docs=None, admin=False,
                 correspondents=None, doc_types=None):
        self.custom_fields = list(fields or [])
        self.tags = list(tags or [])
        self.correspondents = list(correspondents or [])
        self.doc_types = list(doc_types or [])
        self.documents = {d["id"]: d for d in (docs or [])}
        self.admin = admin
        self._next_field_id = max([f["id"] for f in self.custom_fields], default=0) + 1
        self._next_tag_id = max([t["id"] for t in self.tags], default=0) + 1
        self._next_taxo_id = 1000
        self.patches = []  # (doc_id, body) for assertions

    # -- build a PaperlessClient bound to this fake ------------------------
    def client(self, base="http://paperless.test:8000", token="test-token-abc"):
        from paperless_assistant.client import PaperlessClient

        sess = _FakePaperlessSession(self)
        return PaperlessClient(base, token, session=sess)

    # -- routing -----------------------------------------------------------
    def handle(self, method, url, **kw):
        m = method.upper()
        path = url.split("/api/", 1)[1] if "/api/" in url else url
        if path.startswith("custom_fields"):
            return self._custom_fields(m, url, **kw)
        if path.startswith("tags"):
            return self._tags(m, url, **kw)
        if path.startswith("correspondents"):
            return self._taxonomy(m, url, self.correspondents, **kw)
        if path.startswith("document_types"):
            return self._taxonomy(m, url, self.doc_types, **kw)
        if path.startswith("documents"):
            return self._documents(m, url, **kw)
        if path.startswith("ui_settings"):
            user = {"is_superuser": self.admin, "is_staff": self.admin}
            return FakeResponse(200, {"user": user})
        if path in ("", "/") or path.startswith("?"):
            # Mirror real Paperless-NGX: the bare /api/ root 302-redirects and
            # (followed) yields 406 Not Acceptable — it is NOT a usable health
            # probe. A live-stack acceptance caught pa doctor probing it; the
            # fake now rejects it so the offline suite guards against that.
            return FakeResponse(406, {"detail": "Not Acceptable"})
        raise AssertionError(f"FakePaperless: unrouted {m} {url}")

    def _custom_fields(self, m, url, **kw):
        if m == "GET":
            return FakeResponse(200, {"results": list(self.custom_fields), "next": None})
        if m == "POST":
            body = kw.get("json") or {}
            # Mirror real Paperless-NGX: data_type is validated against a fixed
            # set. (A live-stack acceptance caught the provisioner sending the
            # invalid literal "text" instead of "string"; the fake now rejects
            # it too so the offline suite guards this class of bug.)
            valid_types = {"string", "url", "date", "boolean", "integer",
                           "float", "monetary", "documentlink", "select"}
            dt = body.get("data_type")
            if dt not in valid_types:
                return FakeResponse(400, {"data_type": [f'"{dt}" is not a valid choice.']})
            entry = {"id": self._next_field_id, "name": body["name"],
                     "data_type": body["data_type"]}
            if body.get("extra_data"):
                entry["extra_data"] = body["extra_data"]
            self._next_field_id += 1
            self.custom_fields.append(entry)
            return FakeResponse(201, entry)
        raise AssertionError(f"custom_fields {m}")

    def _tags(self, m, url, **kw):
        if m == "GET":
            # ?name__iexact=NAME filter used by tag existence checks.
            if "name__iexact=" in url:
                name = url.split("name__iexact=", 1)[1].split("&")[0]
                hits = [t for t in self.tags if t["name"].lower() == name.lower()]
                return FakeResponse(200, {"results": hits, "next": None})
            return FakeResponse(200, {"results": list(self.tags), "next": None})
        if m == "POST":
            body = kw.get("json") or {}
            entry = {"id": self._next_tag_id, "name": body["name"],
                     "color": body.get("color", "#000000")}
            self._next_tag_id += 1
            self.tags.append(entry)
            return FakeResponse(201, entry)
        raise AssertionError(f"tags {m}")

    def _taxonomy(self, m, url, store, **kw):
        # correspondents / document_types: list + lazy create-if-missing.
        if m == "GET":
            return FakeResponse(200, {"results": list(store), "next": None})
        if m == "POST":
            body = kw.get("json") or {}
            entry = {"id": self._next_taxo_id, "name": body["name"]}
            self._next_taxo_id += 1
            store.append(entry)
            return FakeResponse(201, entry)
        raise AssertionError(f"taxonomy {m}")

    def _documents(self, m, url, **kw):
        # PATCH /api/documents/<id>/
        import re
        mo = re.search(r"/documents/(\d+)/", url)
        if m == "PATCH" and mo:
            doc_id = int(mo.group(1))
            body = kw.get("json") or {}
            self.patches.append((doc_id, body))
            doc = self.documents.get(doc_id, {"id": doc_id})
            doc.update(body)
            self.documents[doc_id] = doc
            return FakeResponse(200, doc)
        if m == "GET" and "/download/" in url and mo:
            return FakeResponse(200, content=b"%PDF-1.4 fake")
        if m == "GET" and mo:
            # Single-document fetch: GET /api/documents/<id>/ (Phase 4 nudge PULL).
            doc_id = int(mo.group(1))
            doc = self.documents.get(doc_id)
            if doc is None:
                return FakeResponse(404, {"detail": "Not found."})
            return FakeResponse(200, doc)
        if m == "GET":
            return FakeResponse(200, {"results": list(self.documents.values()), "next": None})
        raise AssertionError(f"documents {m} {url}")


class _FakePaperlessSession:
    def __init__(self, fake):
        self.fake = fake
        self.headers = {}

    def request(self, method, url, **kw):
        return self.fake.handle(method, url, **kw)

    def post(self, url, **kw):
        return self.fake.handle("POST", url, **kw)


def make_custom_fields(*, stage_options=("triaged", "reocr_done", "metadata_done"),
                       score_type="float", notes_type="string"):
    """Standard, healthy custom-field set for tests."""
    return [
        {"id": 1, "name": "ocr_quality", "data_type": score_type},
        {"id": 2, "name": "ai_stage", "data_type": "select",
         "extra_data": {"select_options": [
             {"id": f"opt-{o[:1]}", "label": o} for o in stage_options]}},
        {"id": 3, "name": "ai_notes", "data_type": notes_type},
    ]


def healthy_tags():
    return [
        {"id": 77, "name": "superseded", "color": "#a0a0a0"},
        {"id": 78, "name": "ai-new-taxonomy", "color": "#f59e0b"},
    ]


class StubUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class StubTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class StubToolUseBlock:
    type = "tool_use"

    def __init__(self, data):
        self.input = data


class StubMessage:
    def __init__(self, content, input_tokens=100, output_tokens=50):
        self.content = content
        self.usage = StubUsage(input_tokens, output_tokens)


class StubAnthropic:
    """Drop-in for anthropic.Anthropic. Configure the message it returns via the
    class-level `next_message` set by tests through `install`.
    """

    _responder = None  # callable(**create_kwargs) -> StubMessage

    def __init__(self, *a, **kw):
        pass

    @property
    def messages(self):
        return self

    def create(self, **kw):
        if StubAnthropic._responder is None:
            raise AssertionError("StubAnthropic responder not installed")
        return StubAnthropic._responder(**kw)


def install_stub_anthropic(monkeypatch, responder):
    """Patch the `anthropic.Anthropic` symbol so `from anthropic import
    Anthropic` inside the engine resolves to our stub."""
    import anthropic

    StubAnthropic._responder = responder
    monkeypatch.setattr(anthropic, "Anthropic", StubAnthropic)


# ===========================================================================
# Stub OpenAI-style provider client (no network, no `openai` package needed).
# Shapes match resp.choices[0].message.content and resp.usage.*.
# ===========================================================================
class _OAIUsage:
    def __init__(self, prompt_tokens=100, completion_tokens=50):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _OAIMessage:
    def __init__(self, content):
        self.content = content


class _OAIChoice:
    def __init__(self, content):
        self.message = _OAIMessage(content)


class _OAIResponse:
    def __init__(self, content, prompt_tokens=100, completion_tokens=50):
        self.choices = [_OAIChoice(content)]
        self.usage = _OAIUsage(prompt_tokens, completion_tokens)


class StubOpenAIClient:
    """Drop-in for openai.OpenAI. `responder(**create_kwargs) -> _OAIResponse`."""

    def __init__(self, responder):
        self._responder = responder

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        return self._responder(**kw)


def make_openai_provider(responder, **overrides):
    """Build an OpenAIProvider whose SDK loader returns a StubOpenAIClient."""
    from paperless_assistant.providers import openai as oai_mod

    kw = dict(
        api_key="sk-openai-test",
        ocr_model="gpt-4o",
        metadata_model="gpt-4o",
        max_ocr_tokens=8000,
    )
    kw.update(overrides)
    prov = oai_mod.OpenAIProvider(**kw)
    # Bypass the real SDK: _client() returns our stub.
    prov._client = lambda: StubOpenAIClient(responder)  # type: ignore[method-assign]
    return prov


# ===========================================================================
# Stub Ollama HTTP transport (no server, no real httpx post).
# ===========================================================================
class _OllamaResp:
    """Faithful-enough stand-in for an httpx.Response from Ollama. Carries the
    `status_code` / `text` the improved error surfacing reads; `json()` returns
    the decoded body (a success payload or an `{"error": ...}` object)."""

    def __init__(self, payload, *, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ollama_error(status_code, error=None, *, text=""):
    """Build a stubbed Ollama HTTP error response mirroring the server's real
    shape: `{"error": "..."}` with a >=400 status. Use as a responder return
    value in `make_ollama_provider`."""
    payload = {"error": error} if error is not None else None
    return _OllamaResp(payload, status_code=status_code, text=text)


def make_ollama_provider(responder, monkeypatch, **overrides):
    """Build an OllamaProvider whose httpx.post is stubbed. `responder(url,
    json=...)` may return either a payload dict (wrapped as a 200 response) or a
    ready `_OllamaResp` (e.g. from `ollama_error(...)`) to simulate an HTTP
    failure with a body."""
    from paperless_assistant.providers import ollama as ol_mod

    kw = dict(ocr_model="llava:13b", metadata_model="llama3.1")
    kw.update(overrides)
    prov = ol_mod.OllamaProvider(**kw)

    def _wrap(result):
        return result if isinstance(result, _OllamaResp) else _OllamaResp(result)

    class _StubHttpx:
        @staticmethod
        def post(url, **kw):
            return _wrap(responder(url, **kw))

    monkeypatch.setattr(ol_mod, "_load_httpx", lambda: _StubHttpx)
    return prov
