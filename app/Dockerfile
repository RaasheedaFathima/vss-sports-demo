FROM python:3.11-slim

# System deps: ffmpeg for frame extraction
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY main.py .

# Frontend static files mounted in at runtime via ConfigMap or volume.
# Override STATIC_DIR env var to point to your index.html directory.
RUN mkdir -p /app/static

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
