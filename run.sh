#!/usr/bin/env bash
# Запуск без Docker (Mac/Linux): ./run.sh  → открыть http://127.0.0.1:8000
# Нужен Postgres: укажите DATABASE_URL в .env (проще — через Docker: docker compose up -d)
set -e
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "Создан .env — впишите TG_API_ID, TG_API_HASH и DATABASE_URL и запустите снова"; exit 1; }
grep -q '^DATABASE_URL=' .env || { echo "В .env нет DATABASE_URL — без Docker нужен свой Postgres"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
