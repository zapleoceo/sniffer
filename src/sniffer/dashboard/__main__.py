"""Процесс дашборда: uvicorn на 8005 внутри общего каркаса `runtime/`.

Каркас нужен ровно за тем же, за чем остальным пяти: без обязательных секретов
процесс не падает, а пишет в лог, чего ждёт, и перепроверяет окружение раз в
минуту (architecture.md, 3.1). Для интерфейса это особенно важно: пустой
`DASHBOARD_SESSION_SECRET` означает «вход выключен», и контейнер, падающий из-за
незаполненного `.env`, выглядел бы как сломанный деплой.

Порт слушаем на всех интерфейсах контейнера, но публикуется он только на
`127.0.0.1` (`docker-compose.yml`): снаружи ходит nginx, а не браузер.
"""

from __future__ import annotations

import asyncio

import structlog
import uvicorn

from sniffer.config import Settings, get_settings
from sniffer.dashboard import auth
from sniffer.dashboard.app import create_app
from sniffer.runtime.service import Service, run_service

log = structlog.get_logger(__name__)

NAME = "dashboard"


def missing_settings(settings: Settings) -> list[str]:
    """Список тот же, что проверяет вход: один источник правды на два места."""
    return auth.missing_settings(settings)


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            # Сигналами владеет runtime: два обработчика на один SIGTERM — это
            # гонка за то, кто первым закроет сервер.
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
        )
    )
    serving = asyncio.create_task(server.serve())
    stopping = asyncio.create_task(stop.wait())
    log.info("dashboard.listening", host=settings.dashboard_host, port=settings.dashboard_port)

    done, _ = await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
    stopping.cancel()
    if serving not in done:
        server.should_exit = True
        await serving


SERVICE = Service(name=NAME, requires=missing_settings, run=run)

if __name__ == "__main__":
    run_service(SERVICE)
