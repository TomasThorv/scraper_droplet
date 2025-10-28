#!/usr/bin/env python
"""
Celery worker startup script for deployment
"""
import os
import sys
import django
from pathlib import Path

# Since we're in the scraper directory in the container, find Django project
current_dir = Path(__file__).parent  # /app (contains scraper contents)
sys.path.insert(0, str(current_dir))
sys.path.insert(0, "/")  # Add root for deployment

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "middleware.settings")

try:
    django.setup()
except Exception as e:
    print(f"Django setup failed: {e}")
    sys.exit(1)

# Import and start Celery
try:
    from middleware.celery import app
except ImportError as e:
    print(f"Failed to import Celery app: {e}")
    sys.exit(1)

if __name__ == "__main__":
    try:
        app.worker_main(
            [
                "worker",
                "--loglevel=info",
                "--concurrency=1",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ]
        )
    except Exception as e:
        print(f"Celery worker failed: {e}")
        sys.exit(1)
