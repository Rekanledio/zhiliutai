#!/bin/sh
set -eu

cd /app/backend
.venv/bin/alembic upgrade head
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
