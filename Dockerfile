FROM python:3.11-bookworm

# Headless Chromium + driver
RUN apt-get update && apt-get install -y \
    chromium chromium-driver \
    libnss3 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER=/usr/bin/chromedriver
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install only scraper deps
COPY scraper/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Copy scraper code
COPY scraper/ /app/scraper/

# Default command (can be overridden in spec)
CMD ["python","-m","celery","-A","scraper.celery_app","worker","-l","info"]
