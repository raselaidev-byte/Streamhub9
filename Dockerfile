# ── Builder stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps for Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="StreamBot"
LABEL description="M3U8 Telegram Recording Bot"

# FFmpeg + curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r streambot && useradd -r -g streambot -d /app streambot

# Copy installed packages
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy source
COPY . .

# Data directory (mount as volume in production)
RUN mkdir -p /data /var/log/streambot \
    && chown -R streambot:streambot /app /data /var/log/streambot

USER streambot

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default: run the bot. Override CMD in docker-compose for worker.
CMD ["python", "-m", "bot.main"]
