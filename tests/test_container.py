"""Offline tests for the container privilege-drop guard (container.py).

The real drop needs root on POSIX; here we monkeypatch the os-level calls so the
control flow is exercised deterministically on any platform (incl. the Windows dev
host). What matters: it is a strict NO-OP unless PA_CONTAINER=1 AND euid==0, and
when it does fire it fixes /data ownership and drops to PA_UID:PA_GID.
"""
import os

import paperless_assistant.container as container


def _stub_priv_calls(monkeypatch):
    calls = {"chown": [], "setgroups": None, "setgid": None, "setuid": None}
    monkeypatch.setattr(os, "chown",
                        lambda p, u, g, **k: calls["chown"].append((p, u, g)),
                        raising=False)
    monkeypatch.setattr(os, "setgroups",
                        lambda g: calls.__setitem__("setgroups", g), raising=False)
    monkeypatch.setattr(os, "setgid",
                        lambda g: calls.__setitem__("setgid", g), raising=False)
    monkeypatch.setattr(os, "setuid",
                        lambda u: calls.__setitem__("setuid", u), raising=False)
    return calls


def test_noop_when_not_container(monkeypatch):
    monkeypatch.delenv("PA_CONTAINER", raising=False)
    calls = _stub_priv_calls(monkeypatch)
    container.drop_privileges_if_container_root()
    assert calls["setuid"] is None  # never dropped


def test_noop_when_container_but_not_root(monkeypatch):
    monkeypatch.setenv("PA_CONTAINER", "1")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    calls = _stub_priv_calls(monkeypatch)
    container.drop_privileges_if_container_root()
    assert calls["setuid"] is None  # not root -> no drop


def test_drops_and_fixes_data_when_container_root(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("PA_CONTAINER", "1")
    monkeypatch.setenv("PA_DATA_DIR", str(data))
    monkeypatch.setenv("PA_UID", "10001")
    monkeypatch.setenv("PA_GID", "10001")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    # Pretend the freshly-created /data is root-owned so the chown path runs.
    monkeypatch.setattr(os, "stat",
                        lambda p, **k: type("S", (), {"st_uid": 0, "st_gid": 0})())
    calls = _stub_priv_calls(monkeypatch)

    container.drop_privileges_if_container_root()

    assert calls["chown"]                # /data ownership fixed (root -> pa)
    assert calls["setgid"] == 10001
    assert calls["setuid"] == 10001      # dropped to the pa user, uid last
