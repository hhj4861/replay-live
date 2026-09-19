"""Vercel commercial API entrypoint (a separate project from the existing web POC)."""
from server.production_app import create_production_app

app = create_production_app()
