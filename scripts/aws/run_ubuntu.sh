#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-aws.txt

export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export EDUCORE_DB="${EDUCORE_DB:-$ROOT_DIR/data/educore.sqlite3}"
export EDUCORE_ENGINE="${EDUCORE_ENGINE:-bedrock}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export EDUCORE_BEDROCK_MODEL_ID="${EDUCORE_BEDROCK_MODEL_ID:-us.amazon.nova-lite-v1:0}"

PORT="${PORT:-8000}"
echo "Iniciando EduAndes AI en http://0.0.0.0:${PORT}/#/ (motor=${EDUCORE_ENGINE})"
exec .venv/bin/python scripts/laptop/servir.py --host 0.0.0.0 --port "$PORT"
