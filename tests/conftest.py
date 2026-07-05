"""Shared pytest fixtures.

The original stageN_*.py scripts do env-var validation and requests.Session()
setup at import time. We set dummy env vars via an autouse fixture and through
os.environ at collection time so both the originals and the package import
cleanly with NO live Paperless and NO real Anthropic key.
"""
import os
import sys
import importlib
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Ensure the original scripts (repo root) and the package (src/) are importable.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

# Set dummy secrets BEFORE any test module imports the originals / package.
os.environ.setdefault("PAPERLESS_URL", "http://paperless.test:8000")
os.environ.setdefault("PAPERLESS_TOKEN", "test-token-abc")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key")


@pytest.fixture(autouse=True)
def _dummy_env(monkeypatch):
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test:8000")
    monkeypatch.setenv("PAPERLESS_TOKEN", "test-token-abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")


def _import_original(name):
    """Import one of the original stageN scripts as a module (cached)."""
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


@pytest.fixture(scope="session")
def orig_stage0():
    return _import_original("stage0_triage")


@pytest.fixture(scope="session")
def orig_stage1():
    return _import_original("stage1_reocr")


@pytest.fixture(scope="session")
def orig_stage2():
    return _import_original("stage2_metadata")


# ---------------------------------------------------------------------------
# Sample OCR text fixtures for the garbage-score characterization tests.
# ---------------------------------------------------------------------------
CLEAN_TEXT = (
    "Dear Mr. Smith,\n\n"
    "Thank you for your payment of $124.50 received on March 3, 2024. "
    "Your account balance is now zero. Please retain this letter for your "
    "records. If you have any questions about this statement, contact our "
    "billing department at your earliest convenience.\n\n"
    "Sincerely,\nAcme Corporation Billing Team"
)

GARBAGE_TEXT = "j f9 3q z x k l m n b v c x z q w e r t y ii oo aa bb cc dd 4 5 6 7 8 9"

EMPTY_TEXT = "   \n  \t "

TINY_TEXT = "hi there"

NO_ALPHA_TEXT = "1234567890 !@#$%^&*() 9876543210 ---- ==== ++++ 5555 6666 7777 8888"

SAMPLE_TEXTS = {
    "clean": CLEAN_TEXT,
    "garbage": GARBAGE_TEXT,
    "empty": EMPTY_TEXT,
    "tiny": TINY_TEXT,
    "no_alpha": NO_ALPHA_TEXT,
}
