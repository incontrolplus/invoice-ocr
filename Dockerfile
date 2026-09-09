# ===========================================================================
# Stage 1: Build & Python Dependencies Stage
# ===========================================================================
FROM python:3.11-slim-bookworm AS builder

# Prevent Python from writing .pyc and buffer output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install compilation prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    pkg-config \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install pinned production dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# ===========================================================================
# Stage 2: Minimal Production Runtime Stage
# ===========================================================================
FROM python:3.11-slim-bookworm AS runtime

LABEL maintainer="Bulgarian Invoice OCR Team" \
      description="Production Tesseract 5 OCR, HITL Dashboard, and Webhook Microservice" \
      version="1.0.0"

# Set runtime environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    OMP_THREAD_LIMIT=1 \
    MAX_OCR_WORKERS=2 \
    DATABASE_URL=sqlite:////data/invoice_ocr.db \
    STORAGE_DIR=/data/stored_documents

# Install Tesseract 5, official Bulgarian/English models, and headless graphics runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Tesseract 5 OCR engine and language data
    tesseract-ocr \
    tesseract-ocr-bul \
    tesseract-ocr-eng \
    tesseract-ocr-osd \
    # Headless graphics, OpenCV and rendering libraries
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libpq5 \
    # Cyrillic fonts for Bulgarian layout rendering
    fonts-dejavu-core \
    fonts-freefont-ttf \
    # Healthcheck utility
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configure tessdata path
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
RUN if [ ! -d "/usr/share/tesseract-ocr/5/tessdata" ] && [ -d "/usr/share/tesseract-ocr/4.00/tessdata" ]; then \
        ln -s /usr/share/tesseract-ocr/4.00/tessdata /usr/share/tesseract-ocr/5/tessdata; \
    fi

# Copy virtualenv from builder stage
COPY --from=builder /opt/venv /opt/venv

# Create unprivileged system user for container security
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

# Create persistent storage directories
RUN mkdir -p /app /data/stored_documents /data/cache && \
    chown -R appuser:appgroup /app /data

WORKDIR /app

# Copy application code into container
COPY --chown=appuser:appgroup invoice_ocr.py .
COPY --chown=appuser:appgroup accounting_export.py .
COPY --chown=appuser:appgroup contractor_verification.py .
COPY --chown=appuser:appgroup database.py .
COPY --chown=appuser:appgroup webhooks.py .
COPY --chown=appuser:appgroup api_server.py .
COPY --chown=appuser:appgroup supabase_sync.py .
COPY --chown=appuser:appgroup invoice_core/ ./invoice_core/
COPY --chown=appuser:appgroup config/ ./config/
COPY --chown=appuser:appgroup vendor_profiles/ ./vendor_profiles/
COPY --chown=appuser:appgroup static/ ./static/
COPY --chown=appuser:appgroup tessdata/ ./tessdata/

# Switch to unprivileged runtime user
USER appuser

# Volume for database and document file persistence
VOLUME ["/data"]

# Expose HTTP REST service port
EXPOSE 8000

# Docker healthcheck checking endpoint readiness and Tesseract engine
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Launch production microservice
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
