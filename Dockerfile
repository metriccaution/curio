# Pinned to 3.12 to match .python-version - pillow-avif-plugin doesn't build on newer.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies in their own layer, before copying the app, so code-only changes
# don't invalidate the dependency cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY server.py sync.py lib.py ./
COPY templates ./templates
RUN uv sync --locked --no-dev

ENV SUBJECTS_DIR=/data/subjects

RUN useradd --create-home --uid 1000 server
USER server

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
