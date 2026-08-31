# Один образ на все пять процессов — они отличаются только командой запуска.
# python:3.12-slim собирается и на amd64, и на arm64, поэтому та же сборка
# едет на Ampere-инстанс Oracle без изменений.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Зависимости отдельным слоем от кода: правка исходников не пересобирает
# установку пакетов, а на слабой ARM-машине это разница в минуты.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --frozen --no-dev

RUN useradd -m -u 1000 sniffer && chown -R sniffer:sniffer /app
USER sniffer

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src

# Порт слушает только `dashboard` (8005) — EXPOSE и healthcheck для него стоят
# в `docker-compose.yml`, а не здесь: образ общий, и остальные четыре процесса
# ничего не слушают. Бот работает long polling.
CMD ["python", "-m", "sniffer.bot"]
