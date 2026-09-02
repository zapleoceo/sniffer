#!/usr/bin/env bash
# Деплой SnifferBot. Скрипт выполняется НА сервере.
#
# Из CI:   workflow копирует этот файл в /tmp и запускает `bash /tmp/... <path> <sha>`
# Руками:  cd /var/www/sniffer && bash infra/deploy.sh
# Проверка без изменений: bash infra/deploy.sh --check
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
FORCE_BUILD=${FORCE_BUILD:-0}
POS1=""
POS2=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check|--preflight) PREFLIGHT_ONLY=1 ;;
    --force-build)       FORCE_BUILD=1 ;;
    -h|--help)
      cat <<'USAGE'
deploy.sh [--check] [--force-build] [DEPLOY_PATH] [TARGET_REF]

  --check        только проверки (диск, окружение), ничего не меняет
  --force-build  пересобрать образ, даже если исходники не менялись

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
# Postgres поднимаем первым и ждём healthy: миграция в неподнятую базу — гонка,
# а app-контейнеры обязаны стартовать уже на новой схеме.
log "миграции схемы"
docker compose up -d postgres
PG_MIG_CID="$(docker compose ps -q postgres 2>/dev/null | head -n1 || true)"
if [ -z "$PG_MIG_CID" ]; then
  die "postgres не поднялся — миграции применить негде" 40
fi
for _ in $(seq 1 30); do
  H="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$PG_MIG_CID" 2>/dev/null || echo none)"
  [ "$H" = "healthy" ] || [ "$H" = "none" ] && break
  sleep 2
done
if docker compose exec -T postgres psql -U sniffer -d sniffer -v ON_ERROR_STOP=1 \
     < infra/sql/001_init.sql >/dev/null; then
  info "схема применена из infra/sql/001_init.sql"
else
  die "миграции схемы не применились — см. ошибку psql выше" 40
fi

# ── 5. Запуск ───────────────────────────────────────────────────────────────
log "запуск"
# --remove-orphans действует внутри compose-проекта sniffer и до контейнеров
# Веры и Степана не дотягивается.
docker compose up -d --remove-orphans

# ── 6. Здоровье ─────────────────────────────────────────────────────────────
log "здоровье (даю ${SETTLE_S}с на прогрев)"
SERVICES="$(docker compose config --services)"

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
  # колонок source/external_id/scan_listing_id не было, и matcher падал. Колонка
  # из миграции единого каталога — часовой того, что ALTER'ы доехали, а не только
  # CREATE TABLE. Появится новая миграция — сюда добавляется её колонка-часовой.
  HAS_COL="$(docker exec "$PG_CID" psql -U sniffer -d sniffer -tAc "select count(*) from information_schema.columns where table_name='listings' and column_name='source'" 2>/dev/null || echo 0)"
  if [ "${HAS_COL:-0}" -lt 1 ]; then
    echo "   миграции не применились: listings.source отсутствует — см. раздел «миграции схемы»" >&2
    FAIL=1
  else
    info "миграции: listings.source на месте"
  fi
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

log "последние $LOG_TAIL строк логов"
docker compose logs --tail "$LOG_TAIL" --no-color --timestamps 2>&1 | tail -n 200 || true

if [ "$FAIL" -ne 0 ]; then
  die "деплой $NEW_SHA прошёл, но контейнеры не в порядке — см. логи выше" 40
fi

log "готово: $NEW_SHA"
