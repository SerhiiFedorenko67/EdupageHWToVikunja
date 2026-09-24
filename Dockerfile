FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/data

COPY --from=builder /opt/venv /opt/venv
RUN mkdir /data && chown 10001:10001 /data

USER 10001:10001
WORKDIR /data
ENTRYPOINT ["edupagetasks"]
CMD ["daemon", "--config", "/data/config.yaml"]
