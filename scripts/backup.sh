#!/bin/sh
# Бэкап панели: дамп Postgres + архив data/ (сессии Telegram, ключ входа). Работает в сервисе backup (docker compose).
#   backup.sh once   — один бэкап сейчас
#   backup.sh        — бэкап сразу и затем раз в сутки
# Хранение: последние KEEP_DAYS дней (по умолчанию 14).
set -eu
KEEP_DAYS="${KEEP_DAYS:-14}"
OUT=/backups
umask 077                                   # в архиве сессии Telegram = доступ к аккаунтам: только владельцу

run() {
  ts=$(date +%Y-%m-%d_%H%M)
  pg_dump -h postgres -U tg -d tg --format=custom --file="$OUT/db_$ts.dump.tmp" && mv "$OUT/db_$ts.dump.tmp" "$OUT/db_$ts.dump"
  tar -czf "$OUT/data_$ts.tgz.tmp" -C / data && mv "$OUT/data_$ts.tgz.tmp" "$OUT/data_$ts.tgz"
  pg_restore --list "$OUT/db_$ts.dump" > /dev/null          # дамп читается
  gzip -t "$OUT/data_$ts.tgz"                                 # архив цел
  find "$OUT" -name 'db_*.dump' -mtime +"$KEEP_DAYS" -delete
  find "$OUT" -name 'data_*.tgz' -mtime +"$KEEP_DAYS" -delete
  echo "$(date '+%F %T') бэкап готов: db_$ts.dump, data_$ts.tgz"
}

run
[ "${1:-}" = "once" ] && exit 0
while true; do sleep 86400; run; done
