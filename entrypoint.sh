#!/bin/sh
# ── entrypoint.sh ────────────────────────────────────────────────────────
# This runs every time the container starts, BEFORE the actual app does.
# "set -e" means: if any command below fails, stop immediately instead of
# continuing on to start a broken app on top of a half-applied migration.
set -e

echo "Running migrations..."
python manage.py migrate --noinput

echo "Starting gunicorn..."
# "exec" replaces this script's process with gunicorn, rather than running
# gunicorn as a child process. This matters for Docker specifically: it
# means gunicorn becomes PID 1, so it receives shutdown signals directly
# from Docker/Render (e.g. on redeploy) instead of the signal getting
# stuck with this script and gunicorn not shutting down cleanly.
exec gunicorn receipt_backend_api.wsgi:application --bind 0.0.0.0:8000 --workers 3