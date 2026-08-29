# Развёртывание на Oracle Cloud Always Free

Сервис живёт на бесплатной ARM-машине Oracle и не выключается. На машине
разработчика не остаётся ничего, кроме кода.

Почему именно Oracle, а не AWS/Neon/serverless — раздел
[«Что используем»](architecture.md#8-что-используем).

## 1. Аккаунт — делает разработчик

Регистрация на <https://signup.cloud.oracle.com>. Понадобится карта для
верификации личности; по Always Free списаний не происходит.

**Важно при регистрации:** домашний регион выбирается один раз и навсегда.
Бери тот, где реально выдают ARM-мощности — во Франкфурте и Амстердаме часто
«out of capacity». Инстанс всегда можно попробовать поднять позже, но регион
уже не поменять.

## 2. Инстанс

Compute → Instances → Create instance:

| Параметр | Значение |
|---|---|
| Image | Canonical Ubuntu 24.04 |
| Shape | `VM.Standard.A1.Flex` — **2 OCPU, 12 GB** (вся Always Free квота ARM) |
| Boot volume | 50 GB хватает; в бесплатную квоту входит до 200 GB |
| SSH key | сгенерировать и **сохранить приватный ключ** — второй раз его не покажут |

Если shape `A1.Flex` недоступен («Out of capacity») — пробовать в другой день
или другой домен доступности внутри региона. Запасной вариант, если ARM так и
не дадут: два `VM.Standard.E2.1.Micro` (AMD), они тоже Always Free, но слабее —
тогда Postgres выносится на отдельный микро-инстанс.

## 3. Сеть

Единственный порт, который нужен снаружи, — **22 (SSH)**, и его стоит
ограничить своим IP в security list подсети.

Ни один процесс не слушает публичный порт: бот работает long polling
(исходящие соединения), Postgres привязан к `127.0.0.1`. Поэтому ни домена, ни
TLS, ни nginx здесь нет — и открывать 80/443 не нужно.

У Ubuntu на Oracle помимо security list есть **локальный iptables**, который по
умолчанию рубит почти всё. Если после открытия порта в консоли соединение всё
равно не идёт — смотреть `sudo iptables -L -n`, а не настройки облака.

## 4. Установка

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
```

```bash
sudo usermod -aG docker ubuntu && newgrp docker
```

```bash
git clone <repo> sniffer && cd sniffer && cp .env.example .env
```

Заполнить `.env` — чеклист в конце
[architecture.md](architecture.md#12-что-нужно-завести). `DATABASE_URL`
поменять на `postgresql+asyncpg://sniffer:<пароль>@postgres:5432/sniffer`:
внутри compose-сети база доступна по имени сервиса, а не по localhost.

```bash
docker compose up -d --build
```

Схема применяется автоматически при первом старте контейнера postgres.

## 5. Первая авторизация юзербота

Telethon при первом запуске запросит код из SMS — интерактивно, поэтому один
раз это делается руками:

```bash
docker compose run --rm collector python -m sniffer.collector auth
```

Полученную `StringSession` вписать в `.env` как `TG_SESSION` и перезапустить.
Хранить строкой, а не файлом: файловая сессия не переживает пересоздание
контейнера.

## 6. Бэкап

`pg_dump` по крону в R2 — этим закрывается единственный минус базы на своей
машине по сравнению с managed-решением:

```bash
0 4 * * * cd /home/ubuntu/sniffer && docker compose exec -T postgres pg_dump -U sniffer sniffer | gzip > /tmp/db.sql.gz && aws s3 cp /tmp/db.sql.gz s3://sniffer-media/backups/$(date +\%F).sql.gz --endpoint-url https://<account>.r2.cloudflarestorage.com
```

**Бэкап, который ни разу не восстанавливали, — это не бэкап.** Раз в месяц
поднять дамп в локальный контейнер и убедиться, что он читается.

## 7. Обновление

```bash
git pull && docker compose up -d --build
```

Простой на время пересборки не теряет объявления: коллектор при старте
догоняет каждый чат с `chats.last_msg_id`.
