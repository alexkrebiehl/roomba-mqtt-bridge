FROM ghcr.io/astral-sh/uv:0.10.7 AS uv

FROM python:3.13-slim

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && chmod -R a+rX /app
USER app

CMD ["/app/.venv/bin/roomba-mqtt-bridge"]
