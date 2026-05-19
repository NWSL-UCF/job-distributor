"""
WSGI entry point for Gunicorn.

Usage:
    gunicorn "hub.wsgi:app" --workers=1 --threads=4 --bind=0.0.0.0:5000

Note: use --workers=1 to prevent duplicate background threads.
For higher concurrency use --threads instead.
"""
from hub.app import create_app

app = create_app()
