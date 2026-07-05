"""Docker image tests (plan §8, r6).

If a Docker daemon is available, actually BUILD the image and assert
`docker run ... pa --help` works. If Docker is NOT available, the build test is
SKIPPED (clearly, not faked) and we fall back to structural assertions on the
Dockerfile that don't need a daemon.

These tests are slow/guarded so they don't block the offline unit layer.
"""
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
IMAGE_TAG = "paperless-assistant:pytest"


def _docker_available():
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "version"], capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Structural checks - always run, no daemon needed.
# ---------------------------------------------------------------------------
def test_dockerfile_is_pure_python_non_root_no_ports():
    text = DOCKERFILE.read_text()
    # Pure-python base, no system OCR deps / apt installs. Ignore comment lines
    # (the Dockerfile deliberately NAMES the banned tools to warn against them).
    code = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    ).lower()
    assert "python:3.12-slim" in text
    for banned in ("apt-get install", "apt install", "ocrmypdf", "tesseract", "poppler"):
        assert banned not in code, f"Dockerfile must not install {banned}"
    # Non-root: a `pa` user exists and the app DROPS to it at runtime (container.py,
    # gated by PA_CONTAINER=1). The container starts as root ONLY to make a
    # bind-mounted /data writable, then drops before any work.
    assert "useradd" in text
    assert "PA_CONTAINER=1" in text
    # `pa` entrypoint.
    assert 'ENTRYPOINT ["pa"]' in text
    # No published ports (ignore the comment that explains why).
    assert "expose" not in code
    # /data volume.
    assert 'VOLUME ["/data"]' in text


def test_dockerignore_excludes_secrets_and_scripts():
    text = (REPO / ".dockerignore").read_text()
    for pat in (".env", ".git", "stage0_triage.py"):
        assert pat in text


# ---------------------------------------------------------------------------
# Real build + smoke test - only when Docker is available.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available")
def test_docker_build_and_help():
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "-f", str(DOCKERFILE), str(REPO)],
        capture_output=True, text=True, timeout=600,
        encoding="utf-8", errors="replace",
    )
    assert build.returncode == 0, f"docker build failed:\n{build.stderr[-3000:]}"

    run = subprocess.run(
        ["docker", "run", "--rm", IMAGE_TAG, "--help"],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    assert run.returncode == 0, f"docker run pa --help failed:\n{run.stderr}"
    out = run.stdout + run.stderr
    for cmd in ("triage", "setup", "doctor", "run", "serve"):
        assert cmd in out

    # The container starts as root ONLY to fix /data perms, then `pa` DROPS to the
    # non-root pa user (uid 10001). Prove the drop by invoking it and reading uid.
    uid = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", IMAGE_TAG, "-c",
         "from paperless_assistant.container import drop_privileges_if_container_root as d;"
         " d(); import os; print(os.getuid())"],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    assert uid.stdout.strip() == "10001", (
        f"expected drop to uid 10001, got {uid.stdout!r}\n{uid.stderr[-800:]}"
    )
