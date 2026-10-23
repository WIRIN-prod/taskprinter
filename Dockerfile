FROM python:3.12-slim

# DejaVu fonts for receipts; bluez client libs are not needed (bleak talks to BlueZ over D-Bus).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    TP_DATA_DIR=/data TP_HOST=0.0.0.0 TP_PORT=8080

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

VOLUME /data
EXPOSE 8080
CMD ["/app/.venv/bin/taskprinter", "serve"]
