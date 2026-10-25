# syntax=docker/dockerfile:1

# Keep the tag in step with PYTHON_VERSION in .github/workflows/checks.yaml, or
# CI tests a different interpreter from the one that ships. Renovate moves both
# in one pull request (the "python" group in renovate.json).

# ---- build stage: install pinned deps into an isolated venv ----
FROM python:3.15-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt

# ---- runtime stage: slim, non-root, no build tooling ----
FROM python:3.15-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
# The templates, stylesheet, script and font live inside the package.
COPY shelly_control/ shelly_control/
# The state directory. Owned by the runtime user, so a Docker named volume
# mounted here starts out writable; in Kubernetes, fsGroup does the same. No
# VOLUME: a run without one keeps its state in memory and says so on the page.
RUN mkdir /data && chown 65532:65532 /data

EXPOSE 9123
# nonroot uid (matches the cluster's runAsNonRoot/runAsUser 65532)
USER 65532:65532
ENTRYPOINT ["python", "-u", "-m", "shelly_control"]
