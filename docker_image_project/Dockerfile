# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATIC_DIR=/app/static \
    UPLOADS_DIR=/data/uploads \
    FASTEMBED_CACHE_DIR=/data/fastembed-cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /tmp/app-requirements.txt
COPY worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir \
    -r /tmp/app-requirements.txt \
    -r /tmp/worker-requirements.txt \
    "oracledb>=2.4,<4"

COPY app/main.py /app/main.py
COPY worker/worker.py /app/worker.py
COPY frontend/index.html /app/static/index.html

RUN mkdir -p /data/uploads /data/fastembed-cache /wallet

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
