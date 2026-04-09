#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

exec uvicorn app:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8088}"
