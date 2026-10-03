#!/usr/bin/env bash
# Резервная копия БД SnifferBot: pg_dump -Fc в контейнере -> каталог копий Веры.
#
# Кто запускает: cron. Расписание ставит шаг «резервная копия БД» в
# infra/deploy.sh (/etc/cron.d/sniffer-backup); руками:
#   bash /var/www/sniffer/infra/backup/sniffer-pg-backup.sh
# Как восстановить и проверить — docs/deploy.md, «Резервная копия БД sniffer».
#
# Куда и кто забирает. NAS владельца забирает ТОЛЬКО /var/backups/vera, по ключу
# с rrsync -ro (чтение одного каталога), под группой verabackup. Поэтому копия
# лежит в /var/backups/vera/sniffer, каталог 0750 группы verabackup, файл 0640:
# в другой каталог NAS не заглянет, а без группы файл не прочитает. Чистку
# забранных копий на сервере делает vera-backup-prune.sh Веры: он стирает наш
# `sniffer-*.dump` после того, как NAS его прочитал. Своё удаление по возрасту
# здесь — только страховка на случай, если NAS молчит (KEEP_DAYS, 7 суток).
#
# Порядок, и он не случаен:
#   1. pg_dump -Fc (собственный сжатый формат, читается pg_restore) внутри
#      контейнера sniffer-postgres, поток прямо в файл: несжатый дамп в памяти
#      и на диске не нужен;
#   2. пишется во временный файл `...dump.part.<pid>`, а не в итоговый:
#      недописанный дамп не должен выглядеть копией ни для человека, ни для NAS;
#   3. проверка: pg_dump вернул 0, размер не меньше MIN_BYTES (пустой дамп при
#      нулевом коде бывает) и `pg_restore -l` читает оглавление архива;
#   4. только после этого файл встаёт на место (rename) и уходят копии старше
#      KEEP_DAYS суток. Сбойный запуск не вправе стереть последнюю хорошую копию.
#
# Журнал — статусы и числа: имя файла, байты, секунды. Содержимое дампа туда не
# попадает никогда: stdout pg_dump уходит в файл. Пароля у скрипта нет: pg_dump
# идёт под ролью sniffer внутри контейнера, по сокету.
#
# Коды выхода: 0 копия готова; 1 снять дамп не удалось; 2 дамп меньше MIN_BYTES;
#              3 архив не читается (pg_restore -l); 4 неверная настройка, нет
#              группы BACKUP_GROUP или каталог недоступен.
set -euo pipefail
umask 027

BACKUP_DIR="${BACKUP_DIR:-/var/backups/vera/sniffer}"
BACKUP_GROUP="${BACKUP_GROUP:-verabackup}"
PG_CONTAINER="${PG_CONTAINER:-sniffer-postgres}"
KEEP_DAYS="${KEEP_DAYS:-7}"
MIN_BYTES="${MIN_BYTES:-4096}"

say() { printf '%s sniffer-pg-backup: %s\n' "$(date '+%F %T')" "$*"; }
die() { say "ОШИБКА: $1" >&2; exit "${2:-1}"; }

# Настройки проверяются ДО любого действия: `KEEP_DAYS=0` стёр бы и свежую копию.
case "$KEEP_DAYS" in ''|*[!0-9]*) die "KEEP_DAYS должен быть целым числом больше нуля" 4 ;; esac
case "$MIN_BYTES" in ''|*[!0-9]*) die "MIN_BYTES должен быть целым неотрицательным числом" 4 ;; esac
KEEP_MIN=$((10#$KEEP_DAYS * 1440))
[ "$KEEP_MIN" -gt 0 ] || die "KEEP_DAYS должен быть больше нуля" 4

# Группа проверяется ДО создания каталога: без неё `install -g` создал бы каталог
# с чужой группой, а NAS не прочитал бы ничего и молчал бы об этом.
getent group "$BACKUP_GROUP" >/dev/null || die "группы $BACKUP_GROUP нет: NAS не прочтёт копию" 4
# На КАЖДОМ запуске, а не только на первом: права каталога обязаны совпадать с
# тем, что ждёт rrsync, даже если их кто-то поменял руками.
install -d -m 0750 -g "$BACKUP_GROUP" "$BACKUP_DIR" || die "каталог $BACKUP_DIR не подготовить" 4

name="sniffer-$(date +%Y%m%d-%H%M).dump"
final="$BACKUP_DIR/$name"
part="$final.part.$$"
trap 'rm -f -- "$part"' EXIT

started=$SECONDS
# Формат -Fc без `-t`: терминал испортил бы бинарный поток.
dump_rc=0
docker exec "$PG_CONTAINER" pg_dump -U sniffer -d sniffer -Fc >"$part" || dump_rc=$?
if [ "$dump_rc" -ne 0 ]; then
  die "pg_dump завершился с кодом $dump_rc: копия не сделана, старые не тронуты" 1
fi

size="$(wc -c <"$part")"
size="${size//[[:space:]]/}"
if [ "$size" -lt "$MIN_BYTES" ]; then
  die "дамп занял $size байт при минимуме $MIN_BYTES: копия отброшена, старые не тронуты" 2
fi
# Читает pg_restore той же версии, что и pg_dump: внутри контейнера, не на хосте.
docker exec -i "$PG_CONTAINER" pg_restore -l <"$part" >/dev/null 2>&1 \
  || die "архив не читается (pg_restore -l): копия отброшена, старые не тронуты" 3

chgrp "$BACKUP_GROUP" "$part" || die "группу $BACKUP_GROUP на файл не выставить" 4
chmod 640 "$part"
mv -f -- "$part" "$final"

# Удаляются только свои файлы (`sniffer-*.dump` в этом каталоге) и только после
# хорошей копии. Недописанные файлы брошенных запусков (убитых так, что trap не
# сработал) живут сутки: за это время работающий запуск успеет закончить свой.
removed="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sniffer-*.dump' ! -name "$name" \
  -mmin "+$KEEP_MIN" -print -delete | wc -l)"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sniffer-*.dump.part.*' -mmin +1440 -delete
kept="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sniffer-*.dump' | wc -l)"
say "OK: $name, $size байт, $((SECONDS - started)) с; удалено старых: ${removed//[[:space:]]/}; копий в каталоге: ${kept//[[:space:]]/}"
