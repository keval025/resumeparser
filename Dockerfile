# Resume Parser - production image (Render web service, or any Docker host).
#
# Contains: Python 3.13, Tesseract OCR + Poppler (scanned PDFs), Playwright Chromium (PDF export).
# Python 3.13 (not 3.11): the pinned, tested dependency set (constraints.txt) needs Python >= 3.12.
#
#   docker build -t resume-parser .
#   docker run -p 8000:8000 --env-file .env resume-parser
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8000

# OCR tools for scanned resumes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (cached between builds while requirements don't change).
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY . .

# Run as an unprivileged user; data/ is only used for local-mode files (production uses Supabase).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app /ms-playwright
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# 1. apply database migrations, 2. start the server (Render sets $PORT).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
