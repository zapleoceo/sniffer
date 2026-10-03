#!/usr/bin/env bash
# Деплой SnifferBot. Скрипт выполняется НА сервере.
#
# Из CI:   workflow копирует этот файл в /tmp и запускает `bash /tmp/... <path> <sha>`
# Руками:  cd /var/www/sniffer && bash infra/deploy.sh
# Проверка без изменений: bash infra/deploy.sh --check
# Сводка логов без деплоя: bash infra/deploy.sh --summary (только счётчики)
#
# Идемпотентен: повторный запуск на том же коммите не пересобирает образ и не
# трогает контейнеры сверх `up -d`.
#
# Коды выхода:
#   10  окружение не готово (нет каталога, .env, docker)
#   15  деплой уже идёт (занят замок)
#   20  на диске нет места — сборка отменена
#   40  контейнеры не поднялись или перезапускаются по кругу
set -euo pipefail

DEPLOY_PATH_DEFAULT=/var/www/sniffer

PREFLIGHT_ONLY=${PREFLIGHT_ONLY:-0}
SUMMARY_ONLY=${SUMMARY_ONLY:-0}
FORCE_BUILD=${FORCE_BUILD:-0}
POS1=""
POS2=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check|--preflight) PREFLIGHT_ONLY=1 ;;
    --force-build)       FORCE_BUILD=1 ;;
    --summary)           SUMMARY_ONLY=1 ;;
    -h|--help)
      cat <<'USAGE'
deploy.sh [--check] [--force-build] [--summary] [DEPLOY_PATH] [TARGET_REF]

  --check        только проверки (диск, окружение), ничего не меняет
  --force-build  пересобрать образ, даже если исходники не менялись
  --summary      только сводка логов контейнеров (счётчики, без строк), ничего не меняет

  DEPLOY_PATH    по умолчанию /var/www/sniffer
  TARGET_REF     коммит или ветка, по умолчанию origin/master
USAGE
      exit 0
      ;;
    -*) echo "неизвестный флаг: $1" >&2; exit 2 ;;
    *)
      if   [ -z "$POS1" ]; then POS1="$1"
      elif [ -z "$POS2" ]; then POS2="$1"
      else echo "лишний аргумент: $1" >&2; exit 2
      fi
      ;;
  esac
  shift
done

DEPLOY_PATH="${POS1:-${DEPLOY_PATH:-$DEPLOY_PATH_DEFAULT}}"
TARGET_REF="${POS2:-${TARGET_REF:-origin/master}}"

# Порог из CLAUDE.md: выше 85% занятости деплой отменяется. На этой машине уже
# ловили переполнение диска build-cache'ем, и падение на середине сборки хуже
# честного отказа: остаются битые слои, которые занимают место дальше.
DISK_LIMIT_PCT="${DISK_LIMIT_PCT:-85}"
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"

# Все четыре сервиса приложения собираются из одного Dockerfile в один тег,
# поэтому сборка нужна ровно одна. `docker compose build` без аргументов
# запустил бы четыре параллельно — на машине с ~1 ГБ свободной памяти это
# гарантированный OOM. Список держится в синхроне с docker-compose.yml.
BUILD_SERVICE="${BUILD_SERVICE:-collector}"

# Пути, изменение которых требует пересборки образа. Правка compose или доков
# пересборки не требует — достаточно `up -d`.
BUILD_TRIGGER_RE='^(src/|Dockerfile$|pyproject\.toml$|uv\.lock$)'

SETTLE_S="${SETTLE_S:-20}"
LOG_TAIL="${LOG_TAIL:-30}"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\n!! %s\n' "$*" >&2; exit "${2:-1}"; }

# ── Сводка логов ────────────────────────────────────────────────────────────
# Вывод этого скрипта попадает в журнал Actions ПУБЛИЧНОГО репозитория, а в логах
# контейнеров лежат id и названия чатов, ключи кандидатов, параметры SQL (строка
# воркера до 600 символов с текстом объявления) и тексты ошибок. Сырые строки
# сюда не выводятся никогда: считаем уровни и названия событий и печатаем только
# числа. Название события берётся лишь если оно похоже на идентификатор (a-z,
# цифры, точка, подчёркивание, дефис, до 60 знаков): всё прочее считается
# значением и не печатается. Подробности - по SSH, там доступ у владельца.
log_summary() {
  local service lines total errors events
  for service in $(docker compose ps --services 2>/dev/null); do
    lines="$(docker compose logs --tail "$LOG_TAIL" --no-color --no-log-prefix "$service" 2>&1 || true)"
    total="$(printf '%s\n' "$lines" | grep -c . || true)"
    errors="$(printf '%s\n' "$lines" \
      | grep -cE '"level": ?"(error|critical)"|(^|[^A-Za-z])(ERROR|FATAL|CRITICAL)[: ]|Traceback' || true)"
    events="$(printf '%s\n' "$lines" \
      | { grep -oE '"event": ?"[^"]*"' || true; } \
      | sed -E 's/^"event": ?"//; s/"$//' \
      | { grep -E '^[a-z0-9_.-]{1,60}$' || true; } \
      | sort | uniq -c | sort -rn | head -3 \
      | awk '{printf "%s%s=%s", sep, $2, $1; sep=", "}')"
    printf '   %-18s строк %-4s ошибок %-3s %s\n' "$service" "$total" "$errors" "${events:+события: $events}"
  done
  info "строки логов здесь не печатаются: подробности по SSH, docker compose logs --tail 100 <сервис>"
}

if [ "$SUMMARY_ONLY" = 1 ]; then
  [ -d "$DEPLOY_PATH" ] || die "нет каталога $DEPLOY_PATH" 10
  cd "$DEPLOY_PATH"
  log "сводка логов (только счётчики)"
  log_summary
  exit 0
fi

# Часовой миграции: колонка или таблица ОБЯЗАНА быть в живой базе, иначе деплой
# красный. Шаблон один на всё, а вызовы идут из таблицы `schema_sentinels`
# ниже: копию проверки можно было ослабить в одном месте и забыть в другом, а
# константу `HAS_NEW=1` на месте проверки никто бы не заметил. Контракт функции
# держит tests/test_deploy_sentinel_run.py: он гоняет её на подставном `docker`
# и убеждается, что отсутствие красит деплой, а присутствие — нет.
#
# Без колонки спрашивается таблица целиком: у таблицы всегда есть хотя бы одна
# колонка, поэтому тот же запрос без фильтра отвечает «таблица есть».
# Ответ, в котором не число, считается отсутствием: `[ x -lt 1 ]` на не-числе
# не падает, а молча уходит в ветку «на месте».
require_column() {
  local table="$1" column="${2:-}" what filter="" found
  what="таблица ${table}"
  if [ -n "$column" ]; then
    what="${table}.${column}"
    filter=" and column_name='${column}'"
  fi
  found="$(docker exec "$PG_CID" psql -U sniffer -d sniffer -tAc "select count(*) from information_schema.columns where table_schema='public' and table_name='${table}'${filter}" 2>/dev/null || echo 0)"
  case "$found" in ''|*[!0-9]*) found=0 ;; esac
  if [ "$found" -lt 1 ]; then
    echo "   миграции не применились: ${what} отсутствует — см. раздел «миграции схемы»" >&2
    FAIL=1
  else
    info "миграции: ${what} на месте"
  fi
}

# Таблица часовых схемы. ОДНА строка на каждую новую таблицу или колонку — и
# больше ничего: ни новой функции, ни нового блока проверки.
#
#   таблица колонка   колонка из `ALTER TABLE … ADD COLUMN IF NOT EXISTS`: часовой
#                     того, что ALTER доехал до ЖИВОЙ базы. `CREATE TABLE IF NOT
#                     EXISTS` существующую таблицу не трогает, колонки там не
#                     появится, а число таблиц этого не покажет (отказ 02.09.2026);
#   таблица           новая таблица целиком (`CREATE TABLE IF NOT EXISTS`). Деньги
#                     и права живут в новых таблицах, и забытая строка — это
#                     таблица, о пропаже которой деплой не скажет.
#
# Хвост цепочки миграций охраняется теми же строками: файл, не доехавший в
# `git pull`, не запустится и ошибки не даст, зато оставит без ответа часового
# своей таблицы. Правило исполняет tests/test_deploy_sentinels.py: ALTER или
# таблица без строки красят сборку, строка с опечаткой или про несуществующее —
# тоже. Комментарий в конце строки и пустые строки допускаются.
schema_sentinels() {
  cat <<'SENTINELS'
listings          source                 # миграция единого каталога, 02.09.2026
users             awaiting_new_request   # `/new`: следующее сообщение открывает поиск
passports         last_used_at           # порядок списка поисков: выбор возвращает поиск
subscriptions     last_scanned_at        # монитор: ротация обхода, кого не смотрели дольше всех
subscriptions     failed_streak          # монитор, карантин: сколько проходов подряд падала
subscriptions     last_error             # монитор, карантин: чем
subscriptions     quarantined_until      # монитор, карантин: до какого времени не трогать
users             bot_blocked_at         # клиент заблокировал бота: нотифаер не шлёт, матчер не ставит в очередь
outbox            last_error             # причина отмены или отказа строки очереди
users             quota_anchor_at        # 010_quota_ledger: якорь периода, он же часовой того, что файл доехал
users             paywall_offered_at     # не чаще раза в сутки предлагаем подписку
client_requests   shown_count            # сколько карточек показано по запросу
client_requests   withheld_count         # сколько удержано лимитом квоты
quota_periods                            # журнал показов: период квоты (010)
offer_views                              # журнал показов: показанные карточки (010)
schema_proposals                         # хвост цепочки на момент введения таблицы (004)
SENTINELS
}

check_schema_sentinels() {
  local table column checked=0
  while read -r table column _; do
    [ -n "$table" ] || continue
    require_column "$table" "$column"
    checked=$((checked + 1))
  done < <(schema_sentinels | sed 's/#.*//')
  # Пустая таблица (стёрли строки, сломали heredoc) не должна быть зелёной: цикл
  # без единого прохода не проверил ничего, а выглядит как «всё на месте».
  if [ "$checked" -eq 0 ]; then
    echo "   таблица часовых пуста — деплой ничего не проверил" >&2
    FAIL=1
  fi
}

# Расписание резервной копии БД. Копию делает infra/backup/sniffer-pg-backup.sh, а в
# cron её ставит ЭТОТ шаг: сервер описывается репозиторием, а не ручной правкой
# crontab (у БД sniffer резервной копии не было вовсе, хотя в неё ложатся платежи
# и права). Файл целиком принадлежит деплою и перезаписывается, если отличается.
#
# Содержимое — одна функция и для записи, и для сверки после неё: ожидаемый текст
# не должен существовать в двух копиях. Скрипт зовётся через `bash`, а не прямо:
# git с Windows не хранит бит исполнения (docs/deploy.md, раздел 6). PATH задан
# явно: у cron он урезан до /usr/bin:/bin, и `docker` в нём не найдётся. Вывод уходит
# в файл журнала на сервере: там статусы и числа, содержимого дампа нет. Время —
# 03:20 по часам сервера, пользователь root (доступ к сокету docker).
backup_cron_content() {
  cat <<CRON
# Управляется infra/deploy.sh (шаг «резервная копия БД»): ручные правки перезапишет следующий деплой.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
20 3 * * * root bash ${DEPLOY_PATH}/infra/backup/sniffer-pg-backup.sh >>/var/log/sniffer-backup.log 2>&1
CRON
}

# Идемпотентная установка расписания. Возвращает 0 или 1 и называет причину отказа
# строкой в stderr; сама ничего не обрывает: вызывающий решает, что делать с отказом
# (шаг деплоя предупреждает, но не падает). FS_ROOT — корень файловой системы:
# пусто на сервере, подставной каталог в тестах (настоящий /etc тесты не трогают).
install_backup_cron() {
  local script="${DEPLOY_PATH}/infra/backup/sniffer-pg-backup.sh"
  local dir="${FS_ROOT:-}/etc/cron.d" target state
  target="${dir}/sniffer-backup"

  # Расписание на несуществующий скрипт тихо падало бы каждую ночь: без скрипта
  # файл не пишется вовсе.
  if [ ! -f "$script" ]; then
    echo "   нет скрипта резервной копии: ${script}" >&2
    return 1
  fi
  # Путь попадает в строку cron как есть: пробел или `;` изменили бы саму команду,
  # а относительный путь указывал бы в никуда — cron стартует не из каталога деплоя.
  case "$DEPLOY_PATH" in
    /*) ;;
    *)
      echo "   путь деплоя должен быть абсолютным: cron стартует из другого каталога" >&2
      return 1 ;;
  esac
  case "$DEPLOY_PATH" in
    *[!A-Za-z0-9_./-]*)
      echo "   путь деплоя содержит знаки, недопустимые в cron-файле" >&2
      return 1 ;;
  esac
  # Каталог /etc/cron.d создаёт пакет cron, а не деплой: нет каталога — нет cron.
  if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
    echo "   cron недоступен: нет каталога ${dir} или права записи в него" >&2
    return 1
  fi
  if [ -f "$target" ] && backup_cron_content | cmp -s - "$target"; then
    info "cron резервной копии: актуален (${target})"
    return 0
  fi
  state="установлен"
  [ -e "$target" ] && state="обновлён"
  # Сначала во временный файл рядом, потом rename: cron читает каталог раз в
  # минуту и не должен увидеть половину файла. Имя с точкой cron пропускает.
  if ! { backup_cron_content >"${target}.new" && chmod 0644 "${target}.new" &&
         mv -f -T "${target}.new" "$target"; } 2>/dev/null; then
    rm -f "${target}.new"
    echo "   не удалось записать ${target}" >&2
    return 1
  fi
  if ! backup_cron_content | cmp -s - "$target"; then
    echo "   ${target} после записи не совпал с ожидаемым" >&2
    return 1
  fi
  info "cron резервной копии: ${state} (ежедневно 03:20, ${target})"
}

# ── 0. Замок: два деплоя одновременно перетрут друг другу рабочее дерево ─────
LOCK_FILE="/var/lock/sniffer-deploy.lock"
if ! : >"$LOCK_FILE" 2>/dev/null; then LOCK_FILE="/tmp/sniffer-deploy.lock"; fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  die "деплой уже идёт (замок $LOCK_FILE занят) — повторить позже" 15
fi

# ── 1. Окружение ────────────────────────────────────────────────────────────
log "окружение"
[ -d "$DEPLOY_PATH" ]      || die "нет каталога $DEPLOY_PATH" 10
cd "$DEPLOY_PATH"
[ -d .git ]                || die "$DEPLOY_PATH не git-репозиторий" 10
[ -f docker-compose.yml ]  || die "нет docker-compose.yml в $DEPLOY_PATH" 10
[ -f .env ]                || die "нет .env в $DEPLOY_PATH — заполнить по .env.example" 10
command -v docker >/dev/null || die "docker не установлен" 10
docker compose version >/dev/null 2>&1 || die "нет docker compose v2" 10

# Профили compose включаются ВСЕ, и это не удобство, а требование проверяемости.
# Сервис за профилем невидим `docker compose config --services`, а из этого
# списка проверка здоровья (раздел 6) берёт, за кем следить. 03.09.2026:
# `agent-collector` падал по кругу — девять перезапусков на разборе настроек, —
# а деплой рапортовал успех, потому что сервиса в списке не было. Хуже:
# `up -d --remove-orphans` без профиля вправе снести такой контейнер как сироту,
# то есть деплой мог и убить работающий сервис.
#
# Выключать сервис профилем не нужно и не задумано: ненастроенный процесс
# ПРОСТАИВАЕТ (`runtime.service` → `service.idle`), а не падает, поэтому
# поднятый с выключенным флагом контейнер проверку здоровья проходит. Значит
# «включён ли сервис» решает его настройка в .env, а профиль решает лишь то,
# участвует ли он в стеке — и участвовать он должен всегда, иначе его отказы
# никто не увидит.
COMPOSE_PROFILE_ARGS="--profile agent-catalog"
info "каталог: $DEPLOY_PATH"
info "цель:    $TARGET_REF"
info "compose: $(docker compose version --short 2>/dev/null || echo '?')"

# ── 2. Диск ─────────────────────────────────────────────────────────────────
usage_pct() { df -P "$1" | awk 'NR==2 {gsub(/%/,"",$5); print $5+0}'; }

disk_worst() {
  local a b root
  a="$(usage_pct "$DEPLOY_PATH" || echo 0)"
  root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  b="$(usage_pct "$root" 2>/dev/null || echo 0)"
  if [ "${a:-0}" -ge "${b:-0}" ]; then echo "${a:-0}"; else echo "${b:-0}"; fi
}

log "диск"
df -h "$DEPLOY_PATH" | sed 's/^/   /'
USED="$(disk_worst)"
info "занято: ${USED}% (порог отмены ${DISK_LIMIT_PCT}%)"

if [ "$USED" -ge "$DISK_WARN_PCT" ] && [ "$PREFLIGHT_ONLY" -eq 0 ]; then
  info "выше ${DISK_WARN_PCT}% — освобождаю висячие образы и старый build-cache"
  # Висячие (untagged) образы и кэш старше недели. Образы, на которые
  # ссылается хоть один контейнер — включая контейнеры Веры и Степана, —
  # docker не трогает, поэтому чужие стеки этим не задеваются.
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
  USED="$(disk_worst)"
  info "после очистки: ${USED}%"
fi

if [ "$USED" -ge "$DISK_LIMIT_PCT" ]; then
  {
    echo "диск занят на ${USED}% при пороге ${DISK_LIMIT_PCT}% — деплой отменён ДО сборки."
    echo
    echo "Что посмотреть:"
    echo "  df -h /"
    echo "  docker system df"
    echo "  du -xh --max-depth=1 /var/lib/docker | sort -h | tail"
    echo
    echo "Что чистить, в этом порядке:"
    echo "  docker builder prune -f --filter until=24h   # кэш сборки, самый жирный"
    echo "  docker image prune -f                        # висячие образы"
    echo "  docker image prune -a -f --filter until=336h  # образы старше 14 дней"
    echo
    echo "Тома НЕ трогать: docker volume prune снесёт базы Веры и Степана."
  } >&2
  exit 20
fi

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  log "preflight пройден, изменений не вносил"
  exit 0
fi

# ── 3. Код ──────────────────────────────────────────────────────────────────
log "код"
PREV_SHA="$(git rev-parse HEAD 2>/dev/null || echo '')"
DIRT="$(git status --porcelain 2>/dev/null || true)"
if [ -n "$DIRT" ]; then
  info "на сервере были локальные правки — будут отброшены:"
  echo "$DIRT" | sed 's/^/     /'
fi

git fetch --prune --quiet origin
git reset -q --hard HEAD
git checkout -q -B master "$TARGET_REF"
NEW_SHA="$(git rev-parse HEAD)"
info "было:  ${PREV_SHA:-—}"
info "стало: $NEW_SHA"
git --no-pager log -1 --format='   %h %s (%an, %ar)'

# ── 4. Сборка — только если менялось то, что попадает в образ ───────────────
log "сборка"
NEED_BUILD=0
if [ "$FORCE_BUILD" -eq 1 ]; then
  NEED_BUILD=1; info "причина: --force-build"
elif [ -z "$PREV_SHA" ]; then
  NEED_BUILD=1; info "причина: первый деплой"
else
  CHANGED="$(git diff --name-only "$PREV_SHA" "$NEW_SHA" 2>/dev/null || echo FORCE)"
  if [ "$CHANGED" = "FORCE" ]; then
    NEED_BUILD=1; info "причина: предыдущий коммит недоступен, сравнить не с чем"
  elif echo "$CHANGED" | grep -qE "$BUILD_TRIGGER_RE"; then
    NEED_BUILD=1
    info "причина: изменилось содержимое образа —"
    echo "$CHANGED" | grep -E "$BUILD_TRIGGER_RE" | sed 's/^/     /'
  fi
fi

if [ "$NEED_BUILD" -eq 0 ]; then
  IMG="$(docker compose config --images "$BUILD_SERVICE" 2>/dev/null | head -n1 || true)"
  if [ -n "$IMG" ] && ! docker image inspect "$IMG" >/dev/null 2>&1; then
    NEED_BUILD=1; info "причина: образа $IMG нет на машине"
  fi
fi

if [ "$NEED_BUILD" -eq 1 ]; then
  docker compose build "$BUILD_SERVICE"
else
  info "образ актуален, пропускаю"
fi

# ── 4.25 Конфигурация публичного каталога ───────────────────────────────────
# `running` недостаточно: runtime намеренно простаивает без обязательного
# секрета. При публичном rollout это создало бы очередь, которую некому
# исполнять. Проверяем ЭФФЕКТИВНОЕ окружение compose в одноразовых контейнерах
# до рестарта клиентского бота; значения секретов наружу не печатаются.
log "конфигурация публичного каталога"
if ! docker compose $COMPOSE_PROFILE_ARGS run --rm --no-deps --entrypoint python bot -c \
  'from sniffer.config import Settings; s=Settings(); raise SystemExit(0 if s.catalog_mode in ("catalog", "listings") and s.agent_collector_enabled and bool(s.broker_project_key.strip()) else 1)'
then
  die "бот не готов к публичному каталогу: проверить CATALOG_MODE, AGENT_COLLECTOR_ENABLED и BROKER_PROJECT_KEY" 40
fi
if ! docker compose $COMPOSE_PROFILE_ARGS run --rm --no-deps --entrypoint python agent-collector -c \
  'from sniffer.config import Settings; s=Settings(); raise SystemExit(0 if s.agent_collector_enabled and bool(s.broker_project_key.strip()) else 1)'
then
  die "agent-collector не готов: проверить AGENT_COLLECTOR_ENABLED и BROKER_PROJECT_KEY" 40
fi
info "бот и agent-collector получили обязательные настройки"

# ── 4.5 Миграции схемы ──────────────────────────────────────────────────────
# infra/sql/001_init.sql идемпотентен (CREATE TABLE IF NOT EXISTS + ALTER … IF
# NOT EXISTS) и ОБЯЗАН применяться на КАЖДОМ деплое. Само по себе это не
# происходило: файл смонтирован в docker-entrypoint-initdb.d, а Postgres
# прогоняет initdb-скрипты ТОЛЬКО при первой инициализации пустого тома, не при
# апгрейде. Живой отказ 02.09.2026: колонки source/external_id/scan_listing_id
# доехали в репозиторий, но не в базу — matcher падал на «scan_listing_id does
# not exist», а деплой рапортовал успех (проверял число таблиц, не колонок).
#
# Файл берём ХОСТОВЫЙ через stdin, а не смонтированный: bind-mount ФАЙЛА держит
# инод с момента старта контейнера, а `git checkout` заменяет файл новым инодом,
# и внутри контейнера остаётся старая версия без свежих ALTER. stdin это обходит.
#
# Применяется вся цепочка: каждый файл `infra/sql/NNN_*.sql` с ТРЁХЗНАЧНЫМ
# номером, по алфавиту. Маска была двузначной («00» и звёздочка), и файл
# `010_*.sql` не применился бы ни здесь, ни в CI, ни в тесте схемы — молча, при
# зелёной сборке. Теперь маска одна на деплой, CI и тесты, а файл вне маски
# красит tests/test_migration_mask.py. Правила имён и содержимого — docs/deploy.md,
# «Миграции: имя файла и маска».
#
# Postgres поднимаем первым и ждём healthy: миграция в неподнятую базу — гонка,
# а app-контейнеры обязаны стартовать уже на новой схеме.
log "миграции схемы"
docker compose $COMPOSE_PROFILE_ARGS up -d postgres
PG_MIG_CID="$(docker compose ps -q postgres 2>/dev/null | head -n1 || true)"
if [ -z "$PG_MIG_CID" ]; then
  die "postgres не поднялся — миграции применить негде" 40
fi
for _ in $(seq 1 30); do
  H="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$PG_MIG_CID" 2>/dev/null || echo none)"
  [ "$H" = "healthy" ] || [ "$H" = "none" ] && break
  sleep 2
done
for migration in infra/sql/[0-9][0-9][0-9]_*.sql; do
  if docker compose exec -T postgres psql -U sniffer -d sniffer -v ON_ERROR_STOP=1 \
       < "$migration" >/dev/null; then
    info "схема применена из $migration"
  else
    die "миграция $migration не применилась — см. ошибку psql выше" 40
  fi
done

# ── 4.75 Резервная копия БД ─────────────────────────────────────────────────
# Шаг не имеет права ронять деплой: приложение к этому моменту уже обновлено, а
# без cron нужно предупредить, а не откатывать. Но и промолчать он не вправе:
# без копии БД существует в одном экземпляре. Поэтому предупреждение громкое —
# stderr, аннотация Actions (`::warning::` в журнале попадает на страницу
# прогона) и повтор последней строкой деплоя, — а код выхода не меняется.
# `if !` обязателен: голый вызов под `set -e` оборвал бы деплой кодом, которого
# нет в таблице кодов выхода.
log "резервная копия БД"
BACKUP_WARN=0
if ! install_backup_cron; then
  BACKUP_WARN=1
  {
    echo "   ВНИМАНИЕ: резервная копия БД НЕ стоит в расписании — причина строкой выше."
    echo "   Деплой продолжается, код выхода не меняется. Чинить: docs/deploy.md, «Резервная копия БД sniffer»."
  } >&2
  echo "::warning::резервная копия БД sniffer не поставлена в расписание — docs/deploy.md, «Резервная копия БД sniffer»"
fi

# ── 5. Запуск ───────────────────────────────────────────────────────────────
log "запуск"
# --remove-orphans действует внутри compose-проекта sniffer и до контейнеров
# Веры и Степана не дотягивается.
docker compose $COMPOSE_PROFILE_ARGS up -d --remove-orphans

# ── 6. Здоровье ─────────────────────────────────────────────────────────────
log "здоровье (даю ${SETTLE_S}с на прогрев)"
SERVICES="$(docker compose $COMPOSE_PROFILE_ARGS config --services)"

snapshot() {
  local svc cid
  for svc in $SERVICES; do
    cid="$(docker compose ps -q "$svc" 2>/dev/null | head -n1 || true)"
    if [ -z "$cid" ]; then echo "$svc missing 0"; continue; fi
    echo "$svc $(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$cid")"
  done
}

BEFORE="$(snapshot)"
sleep "$SETTLE_S"
AFTER="$(snapshot)"

FAIL=0
while read -r svc status restarts; do
  [ -n "${svc:-}" ] || continue
  prev="$(echo "$BEFORE" | awk -v s="$svc" '$1==s {print $3}')"
  case "$status" in
    running)
      if [ -n "${prev:-}" ] && [ -n "${restarts:-}" ] && [ "$restarts" -gt "$prev" ]; then
        echo "   $svc: перезапустился за время проверки ($prev → $restarts) — падает по кругу" >&2
        FAIL=1
      else
        info "$svc: running"
      fi
      ;;
    missing)  echo "   $svc: контейнера нет" >&2; FAIL=1 ;;
    *)        echo "   $svc: $status" >&2; FAIL=1 ;;
  esac
done <<< "$AFTER"

PG_CID="$(docker compose ps -q postgres 2>/dev/null | head -n1 || true)"
if [ -n "$PG_CID" ]; then
  PG_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$PG_CID")"
  info "postgres healthcheck: $PG_HEALTH"
  if [ "$PG_HEALTH" = "unhealthy" ]; then FAIL=1; fi
fi

# ── 6.5 Функциональная проверка ──────────────────────────────────────────────
# Контейнер в состоянии running ещё ничего не доказывает: процесс может стоять,
# схема не примениться, а образ собраться из чужого коммита. Поэтому деплой
# считается успешным только после того, как система ответила на реальные
# вопросы. Любой провал здесь — красный деплой, а не предупреждение.
log "функциональная проверка"

# 1. Задеплоен именно тот коммит, который просили.
ACTUAL_SHA="$(git -C "$DEPLOY_PATH" rev-parse HEAD)"
EXPECTED_SHA="$(git -C "$DEPLOY_PATH" rev-parse "$TARGET_REF" 2>/dev/null || echo "")"
if [ -n "$EXPECTED_SHA" ] && [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  echo "   код на сервере не тот: ждали ${EXPECTED_SHA}, на диске ${ACTUAL_SHA}" >&2
  FAIL=1
else
  info "коммит: ${ACTUAL_SHA}"
fi

# 2. База отвечает и схема на месте. Пустой список таблиц означает, что
#    init-скрипт не отработал, и бот упадёт на первом же запросе.
if [ -n "${PG_CID:-}" ]; then
  TABLES="$(docker exec "$PG_CID" psql -U sniffer -d sniffer -tAc     "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null || echo 0)"
  if [ "${TABLES:-0}" -lt 5 ]; then
    echo "   схема БД пуста или неполна: таблиц ${TABLES:-0}, ожидалось не меньше 5" >&2
    FAIL=1
  else
    info "схема БД: ${TABLES} таблиц"
  fi
  # Число таблиц не ловит непринятую МИГРАЦИЮ: 02.09.2026 таблицы были, а
  # колонок source/external_id/scan_listing_id не было, и matcher падал. Поэтому
  # деплой спрашивает базу о каждой строке таблицы `schema_sentinels` (вверху
  # скрипта): колонка из свежего ALTER — часовой того, что ALTER'ы доехали, а не
  # только CREATE TABLE; новая таблица — часовой того, что доехал её файл. ЛЮБАЯ
  # новая колонка или таблица в infra/sql обязана получить там свою строку:
  # tests/test_deploy_sentinels.py красит сборку, если строки нет и если она
  # стоит на несуществующем. Бот читает `users` и `passports` на КАЖДОМ
  # сообщении, и отсутствие колонки там означает не деградацию, а молчащий бот
  # при зелёном деплое. Цикл миграций выше доказывает лишь то, что psql не
  # вернул ошибку на ЗАПУЩЕННОМ файле: файл, не попавший в `git pull`, не
  # запустится вовсе и ошибки не даст — его выдаёт только строка про его таблицу.
  check_schema_sentinels
fi

# 3. Образ рабочий: код импортируется. Ловит битую сборку и сломанные
#    зависимости до того, как их поймает клиент.
if docker compose run --rm --no-deps -T bot python -c "import sniffer, sniffer.search.planner, sniffer.sources.base, sniffer.dashboard.app" >/dev/null 2>&1; then
  info "импорт модулей: ок"
else
  echo "   образ собран, но модули не импортируются" >&2
  FAIL=1
fi

# 4. Интерфейс отвечает по HTTP. Контейнер в состоянии running ничего не
#    доказывает: uvicorn мог не подняться, а порт мог не опубликоваться.
#    Спрашиваем /healthz на loopback — снаружи порт закрыт, снаружи ходит nginx.
#    Пробуем до 10 раз: процесс мог ещё догружать зависимости.
DASHBOARD_URL="${DASHBOARD_URL:-http://127.0.0.1:8005/healthz}"
DASHBOARD_OK=0
for _ in $(seq 1 10); do
  if HEALTH_BODY="$(curl -fsS --max-time 5 "$DASHBOARD_URL" 2>/dev/null)"; then
    DASHBOARD_OK=1
    break
  fi
  sleep 3
done
if [ "$DASHBOARD_OK" -eq 1 ]; then
  info "интерфейс отвечает: ${HEALTH_BODY}"
  # `missing` в ответе — это незаполненный .env, а не поломка сборки: процесс
  # ждёт конфигурации и не падает. Деплой не роняем, но говорим об этом громко.
  case "$HEALTH_BODY" in
    *'"missing":[]'*|*'"missing": []'*) : ;;
    *) echo "   ВНИМАНИЕ: интерфейс поднят, но не настроен — ${HEALTH_BODY}" >&2 ;;
  esac
else
  echo "   интерфейс не ответил на ${DASHBOARD_URL}" >&2
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  echo >&2
  echo "ДЕПЛОЙ НЕ ПРОШЁЛ ПРОВЕРКУ — состояние выше" >&2
  exit 40
fi
log "проверка пройдена: деплой успешен"

# ── 7. Уборка ───────────────────────────────────────────────────────────────
log "уборка"
docker image prune -f 2>&1 | tail -n1 | sed 's/^/   /' || true
docker builder prune -f --filter until=168h 2>&1 | tail -n1 | sed 's/^/   /' || true

# ── 8. Статус ───────────────────────────────────────────────────────────────
log "статус"
docker compose ps || true
free -m | sed 's/^/   /' || true
df -h "$DEPLOY_PATH" | sed 's/^/   /' || true

log "сводка логов (только счётчики)"
log_summary

if [ "$FAIL" -ne 0 ]; then
  die "деплой $NEW_SHA прошёл, но контейнеры не в порядке — см. логи выше" 40
fi

# Предупреждение шага 4.75 посреди журнала тонет: повторяем его перед последней строкой.
if [ "${BACKUP_WARN:-0}" -ne 0 ]; then
  echo "ВНИМАНИЕ: деплой прошёл, но резервная копия БД не в расписании (см. шаг «резервная копия БД») — БД остаётся в одном экземпляре" >&2
fi

log "готово: $NEW_SHA"
