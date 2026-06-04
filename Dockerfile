# syntax=docker/dockerfile:1.7
# Debian trixie ships SQLite 3.46+; bookworm's 3.40 hits a DBOS schema-migration
# IntegrityError on first run. Stay on trixie until upstream pins or fixes.
FROM ghcr.io/astral-sh/uv:python3.13-trixie
LABEL org.opencontainers.image.source="https://github.com/raniendu/vikram"
LABEL org.opencontainers.image.description="Spec-driven Pydantic AI agent runtime - CLI + FastAPI HTTP surface."
LABEL org.opencontainers.image.licenses="MIT"

ENV UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    VIKRAM_DB_PATH=/app/.vikram/vikram.sqlite3

WORKDIR /app

# Resolve runtime dependencies first so this layer caches across code edits.
# --no-install-project skips building the vikram wheel; source isn't here yet.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

# Application code and agent specs. Tests/evals/docs are excluded via .dockerignore.
COPY vikram ./vikram
COPY spec ./spec

# Now install the vikram project itself; deps layer above is cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# DBOS + ThreadStore live under /app/.vikram by default; mount a volume here to persist state.
RUN mkdir -p /app/.vikram

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "vikram.api:app", "--host", "0.0.0.0", "--port", "8000"]
