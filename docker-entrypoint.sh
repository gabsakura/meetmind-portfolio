#!/bin/sh
set -eu

echo "Applying database migrations..."
flask --app run.py db upgrade

if [ "${PORTFOLIO_DEMO_ENABLED:-false}" = "true" ]; then
  echo "Ensuring synthetic portfolio data..."
  flask --app run.py seed-demo
fi

exec gunicorn \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  run:app
