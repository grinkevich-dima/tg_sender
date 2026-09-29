#!/bin/bash
# Двойной клик — собрать и запустить TG Sender в Docker
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
cd "$(dirname "$0")"
{
  echo "=== $(date) ==="
  docker compose up -d --build 2>&1
  echo "--- status ---"
  docker compose ps 2>&1
  sleep 5
  docker compose logs --tail 30 2>&1
  echo "=== DONE ==="
} | tee start.log
echo
echo "Панель: http://127.0.0.1:8000  (окно можно закрыть)"
