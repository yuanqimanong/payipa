# pyp-agent self-contained image. Build context is the payipa repository root.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps ./apps
COPY packages ./packages
RUN uv sync --frozen --no-dev --package pyp-agent --no-editable

FROM python:3.14-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
RUN useradd --system --create-home --uid 10001 payipa \
 && mkdir -p /state \
 && chown -R payipa:payipa /state /app
USER payipa
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["pyp-agent"]
CMD ["--help"]
