FROM ghcr.io/astral-sh/uv:0.8.15-python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    BOT_CONFIG=/app/config/config.yaml.example

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . /app

RUN groupadd --system books-of-time \
    && useradd --system --gid books-of-time --home-dir /app books-of-time \
    && mkdir -p /var/lib/books-of-time/raw /var/lib/books-of-time/media /var/lib/books-of-time/accounts \
    && chown -R books-of-time:books-of-time /app /var/lib/books-of-time

USER books-of-time

VOLUME ["/var/lib/books-of-time/raw", "/var/lib/books-of-time/media", "/var/lib/books-of-time/accounts"]

HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=3 \
    CMD ["/app/.venv/bin/python", "main.py", "service", "health"]

CMD ["/app/.venv/bin/python", "main.py", "service", "run"]
