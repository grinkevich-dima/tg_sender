#!/usr/bin/env bash
# Прогон тестов в Docker с настоящим Postgres. Файл-образец списка: SAMPLE_XLSX=~/путь/к/файлу.xlsx scripts/test.sh
set -e
cd "$(dirname "$0")/.."
args=()
if [ -n "$SAMPLE_XLSX" ]; then
  args+=(-v "$SAMPLE_XLSX:/sample.xlsx:ro" -e SAMPLE_XLSX=/sample.xlsx)
fi
docker compose --profile test run --rm --build "${args[@]}" tests python -m pytest -q -p no:cacheprovider tests "$@"
