#!/usr/bin/env bash
# Прогон тестов в Docker с настоящим Postgres. Аргументы передаются pytest: scripts/test.sh -k xlsx
set -e
cd "$(dirname "$0")/.."
docker compose --profile test run --rm --build tests python -m pytest -q -p no:cacheprovider tests "$@"
