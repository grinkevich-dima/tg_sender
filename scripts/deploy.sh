#!/usr/bin/env bash
# Выкладка рабочей панели: собрать образ из текущего кода, перезапустить, дождаться, что панель отвечает.
#   scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "Ветка: $(git branch --show-current)  коммит: $(git log --oneline -1)"
if [ -n "$(git status --porcelain -- app)" ]; then
  echo "В app/ есть незакоммиченные изменения — они тоже попадут в образ:"; git status --short -- app
fi
docker compose up -d --build tg-sender
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/login || true)
  [ "$code" = "200" ] && { echo "Панель отвечает (200)."; docker compose logs --since 2m tg-sender | grep -E "Traceback|ERROR|\[error\]|подключён" | tail -5; exit 0; }
  sleep 2
done
echo "Панель не ответила за минуту — смотрите: docker compose logs tg-sender"; exit 1
