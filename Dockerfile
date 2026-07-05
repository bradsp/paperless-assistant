# Paperless Assistant - slim, pure-python container (plan §8).
#
# PURE-PYTHON: no system dependencies. No OCRmyPDF / tesseract / poppler / apt
# packages beyond the minimal python base. The pure-python PDF overlay
# (pypdf + reportlab) is what makes this possible (plan §4.2, §8) - do NOT add
# system OCR tooling here.
#
# Non-root, no published ports (the agent is outbound/LAN-only, plan §8.1).
# The single /data volume holds snapshots, config, run reports, and the cursor.

# --- build stage: build a wheel so the final image carries no build toolchain
FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist

# --- runtime stage: minimal, non-root -------------------------------------
FROM python:3.12-slim AS runtime

# Don't buffer stdout/stderr so JSON logs stream promptly; no .pyc clutter.
# PA_CONTAINER=1 tells `pa` it may fix /data ownership and drop to the pa user
# (see container.py); PA_UID/PA_GID are the non-root user it drops to.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PA_DATA_DIR=/data \
    PA_CONTAINER=1 \
    PA_UID=10001 \
    PA_GID=10001

# Create a non-root user and the /data mount point it owns.
RUN useradd --create-home --uid 10001 pa \
    && mkdir -p /data \
    && chown -R pa:pa /data

# Install the built wheel WITH the BYO-provider extras so EVERY provider that is
# selectable in the dashboard (anthropic — a base dep — plus openai and ollama)
# works out of the box. These are pure-python wheels: still no compilers / apt.
# The user only needs to supply the matching key (e.g. OPENAI_API_KEY) / endpoint.
COPY --from=build /dist/*.whl /tmp/
RUN whl="$(ls /tmp/*.whl)" \
    && pip install --no-cache-dir "${whl}[openai,ollama]" \
    && rm -f /tmp/*.whl

WORKDIR /home/pa
VOLUME ["/data"]

# The container starts as root ONLY so `pa` can make a bind-mounted /data writable
# (bind mounts arrive owned by the host's root); `pa` then IMMEDIATELY drops to the
# non-root pa user before any work (container.py, gated by PA_CONTAINER=1). This
# fires for the main process AND `docker exec ... pa ...`, so /data is never left
# with root-owned files. The default command runs scheduled sweeps; the first
# processing run defaults to a bounded dry-run with a report (I7).
ENTRYPOINT ["pa"]
CMD ["serve"]

# NOTE: intentionally NO `EXPOSE` / no published ports - outbound/LAN-only.
