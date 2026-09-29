#!/usr/bin/env bash
# Запуск на Mac/Linux: ./run.sh  → открыть http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "Создан .env — впишите TG_API_ID и TG_API_HASH и запустите снова"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
