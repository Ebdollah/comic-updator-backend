# Use Python 3.11 slim image for smaller size
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set work directory
WORKDIR /app

# System dependencies.
#
# Chromium and Xvfb are here for the Cloudflare challenge on en-thunderscans.com
# (see app/services/cloudflare.py). The browser has to run headed - a headless
# Chrome announces itself in its User-Agent and never clears the challenge - so
# it needs a display, which Xvfb provides. The font and library packages are
# what let Chromium start and render at all in a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        chromium \
        xvfb \
        fonts-liberation \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

# CHROME_PATH feeds settings.chrome_path; DISPLAY points at the Xvfb screen
# started by the CMD below.
ENV CHROME_PATH=/usr/bin/chromium \
    DISPLAY=:99

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user for security
RUN adduser --disabled-password --gecos '' appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/api/v1/health || exit 1

# Run the application under a virtual display, so the challenge solver has a
# screen to open a headed Chromium on.
CMD sh -c "xvfb-run -a --server-args='-screen 0 1280x1024x24' gunicorn main:app -k uvicorn.workers.UvicornWorker -w 1 --bind 0.0.0.0:${PORT:-8000}"
