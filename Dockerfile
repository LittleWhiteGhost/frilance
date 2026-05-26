FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime deps. lxml/aiosqlite/aiohttp ship as wheels for python 3.11
# on slim, so no compiler toolchain is required. Keeping the image minimal
# reduces both attack surface and pull time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persisted SQLite lives in /app/data; create it eagerly so the dir exists
# even when no volume is mounted.
RUN mkdir -p /app/data && chown -R nobody:nogroup /app

USER nobody

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sqlite3, os; \
sqlite3.connect(os.getenv('DATABASE_PATH', 'data/bot.db')).execute('SELECT 1')" \
        || exit 1

CMD ["python", "-m", "bot.main"]
