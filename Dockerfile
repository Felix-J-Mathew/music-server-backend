FROM python:3.10.14-slim-bookworm

WORKDIR /app

# Install FFmpeg for audio transcoding
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application code
COPY app.py .
COPY google_client.py .

# Create non-root user
RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8000

# Support dynamic PORT from cloud providers
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]