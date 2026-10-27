FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium \
    DATA_DIR=/app/data \
    HEADLESS=true

WORKDIR /app

RUN set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    apt-get -o Acquire::Retries=5 update; \
    attempt=1; \
    while ! apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        ca-certificates \
        chromium \
        fonts-liberation \
        fonts-noto-color-emoji; do \
        if [ "$attempt" -ge 5 ]; then exit 1; fi; \
        attempt=$((attempt + 1)); \
        sleep 5; \
        apt-get -o Acquire::Retries=5 update; \
    done; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py tracker.py task_store.py airport_catalog.py calendar_ui.py ./
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
