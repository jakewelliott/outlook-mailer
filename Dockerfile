# syntax=docker/dockerfile:1

# ---- builder: install the runtime dependencies into a venv ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Only pyproject.toml is copied here, so this layer is rebuilt only when the dependencies change.
# The runtime dependency list is read straight from it (no dev extras, no editable install).
WORKDIR /build
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt \
    && pip uninstall -y pip

# ---- final image: venv + app source, no pip, build tooling or compilers ----
FROM python:3.12-slim

LABEL org.opencontainers.image.title="outlook-mailer" \
      org.opencontainers.image.description="Rate-limited Markdown email campaigns from a personal Outlook.com account via Microsoft Graph." \
      org.opencontainers.image.source="https://github.com/jakewelliott/outlook-mailer" \
      org.opencontainers.image.url="https://github.com/jakewelliott/outlook-mailer"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/data \
    PORT=8765 \
    TZ=America/New_York

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app/ ./app/

# The venv's pip was removed in the builder; also remove the base image's system pip, which nothing uses at runtime.
# /data holds all state. Owned by 99:100 (Unraid nobody:users) so a fresh named volume is writable.
RUN /usr/local/bin/python -m pip uninstall -y --root-user-action=ignore pip \
    && mkdir -p /data && chown 99:100 /data
VOLUME /data

USER 99:100
EXPOSE 8765

# No curl in the image; Python's urllib does the probe. Any error or non-2xx status exits non-zero.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (os.environ.get('PORT') or '8765'), timeout=4)"]

# Exactly one worker: the send worker runs in-process and must not be duplicated.
#
# --proxy-headers / --forwarded-allow-ips: the app sits behind cloudflared, which connects from
# wherever it runs (another container on a Docker network, or the host), so its source address is
# not fixed and by default we trust X-Forwarded-For/-Proto from any peer ("*"). With "*" uvicorn
# reports the LEFTMOST X-Forwarded-For entry as the client address, and the client controls it
# (Cloudflare appends to an existing X-Forwarded-For rather than replacing it). So the reported
# client IP is not trustworthy: never use it for rate limiting or any security decision (the
# unsubscribe rate limiter keys on CF-Connecting-IP instead; see docs/PLAN.md). Admin auth relies
# on the Cloudflare Access JWT, not on IP, and URLs are built from PUBLIC_BASE_URL, not from the
# forwarded scheme/host, so neither is affected. Set FORWARDED_ALLOW_IPS to cloudflared's address
# or Docker subnet (comma-separated IPs/CIDRs) to tighten it.
#
# Shell form so ${PORT} expands; exec so uvicorn is PID 1 and receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8765}\" --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\""]
