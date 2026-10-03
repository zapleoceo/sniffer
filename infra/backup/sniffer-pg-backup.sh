#!/usr/bin/env bash
# Резервная копия БД SnifferBot: pg_dump в контейнере -> gzip -> каталог копий.
#
# Кто запускает: cron. Расписание ставит шаг «резервная копия БД» в
# infra/deploy.sh (/etc/cron.d/sniffer-backup); руками:
#   bash /var/www/sniffer/infra/backup/sniffer-pg-backup.sh
# Как восстановить и проверить — docs/deploy.md, «Резервная копия БД sniffer».
#
# Порядок, и он не случаен:
#   1. pg_dump внутри контейнера sniffer-postgres, поток сразу в gzip: несжатый
#      дамп на диск не ложится (одна raw_messages — сотни мегабайт);
#   2. пишется во временный файл `...sql.gz.part.<pid>`, а не в итоговый:
#      недописанный дамп не должен выглядеть копией ни для человека, ни для NAS,
#      который забирает каталог;
#   3. проверка: оба конца конвейера вернули 0 (одного `pipefail` мало: нужны
#      коды по отдельности), размер не меньше MIN_BYTES (пустой дамп при нулевом
#      коде возврата бывает, и на такую «копию» полагаются) и архив читается;
#   4. только после этого файл встаёт на место (rename) и уходят копии старше
#      KEEP_DAYS суток. Сбойный запуск не вправе стереть последнюю хорошую копию.
#
# Журнал — статусы и числа: имя файла, байты, секунды. Содержимое дампа туда не
# попадает никогда: stdout pg_dump уходит в gzip. Пароля у скрипта нет: pg_dump
# идёт под ролью sniffer внутри контейнера, по сокету.
#
# Коды выхода: 0 копия готова; 1 снять дамп не удалось; 2 дамп меньше MIN_BYTES;
#              3 архив не читается; 4 неверная настройка или каталог недоступен.
set -euo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/sniffer}"
PG_CONTAINER="${PG_CONTAINER:-sniffer-postgres}"
KEEP_DAYS="${KEEP_DAYS:-3}"
MIN_BYTES="${MIN_BYTES:-4096}"

say() { printf '%s sniffer-pg-backup: %s\n' "$(date '+%F %T')" "$*"; }
die() { say "ОШИБКА: $1" >&2; exit "${2:-1}"; }

# Настройки проверяются ДО любого действия: `KEEP_DAYS=0` стёр бы и свежую копию.
case "$KEEP_DAYS" in ''|*[!0-9]*) die "KEEP_DAYS должен быть целым числом больше нуля" 4 ;; esac
case "$MIN_BYTES" in ''|*[!0-9]*) die "MIN_BYTES должен быть целым неотрицательным числом" 4 ;; esac
KEEP_MIN=$((10#$KEEP_DAYS * 1440))
[ "$KEEP_MIN" -gt 0 ] || die "KEEP_DAYS должен быть больше нуля" 4

mkdir -p "$BACKUP_DIR" || die "каталог $BACKUP_DIR не создать" 4
chmod 700 "$BACKUP_DIR" || die "права на $BACKUP_DIR не выставить" 4

name="sniffer-$(date +%Y%m%d-%H%M).sql.gz"
final="$BACKUP_DIR/$name"
part="$final.part.$$"
trap 'rm -f -- "$part"' EXIT

started=$SECONDS
# `set +e` на время конвейера: нужны коды обоих его концов, а не один общий.
set +e
docker exec "$PG_CONTAINER" pg_dump -U sniffer -d sniffer | gzip -c >"$part"
codes=("${PIPESTATUS[@]}")
set -e
if [ "${codes[0]}" -ne 0 ] || [ "${codes[1]}" -ne 0 ]; then
  die "pg_dump завершился с кодом ${codes[0]}, gzip с кодом ${codes[1]}: копия не сделана, старые не тронуты" 1
fi

size="$(wc -c <"$part")"
size="${size//[[:space:]]/}"
if [ "$size" -lt "$MIN_BYTES" ]; then
  die "дамп занял $size байт при минимуме $MIN_BYTES: копия отброшена, старые не тронуты" 2
fi
gzip -t "$part" 2>/dev/null || die "архив не читается (gzip -t): копия отброшена, старые не тронуты" 3

chmod 600 "$part"
mv -f -- "$part" "$final"

# Удаляются только свои файлы (`sniffer-*.sql.gz` в этом каталоге) и только после
# хорошей копии. Недописанные файлы брошенных запусков (убитых так, что trap не
# сработал) живут сутки: за это время работающий запуск успеет закончить свой.
removed="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sniffer-*.sql.gz' ! -name "$name" \
  -mmin "+$KEEP_MIN" -print -delete | wc -l)"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sniffer-*.sql.gz.part.*' -mmin +1440 -delete
kept="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sniffer-*.sql.gz' | wc -l)"
say "OK: $name, $size байт, $((SECONDS - started)) с; удалено старых: ${removed//[[:space:]]/}; копий в каталоге: ${kept//[[:space:]]/}"
