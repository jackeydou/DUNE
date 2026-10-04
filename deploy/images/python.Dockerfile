# syntax=docker/dockerfile:1
# The Python services: one image, one entry point each (swarmeval-control, swarmeval-worker,
# swarmeval-model-gateway). Build from the repo root:
#   docker build -f deploy/images/python.Dockerfile -t swarmeval/python .

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a change to swarmeval/ rebuilds only the last layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY swarmeval ./swarmeval
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
# The same uid as the Go image, so both read the certificates swarm-certs writes.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin swarmeval
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
