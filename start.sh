#!/usr/bin/env bash
set -euo pipefail

exec gunicorn \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  web_app:app
