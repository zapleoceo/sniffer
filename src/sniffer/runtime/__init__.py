"""Каркас процесса: то, что одинаково у бота, коллектора, воркера и нотифаера.

Слоем не является: бизнес-логики здесь нет, только запуск, сигналы и лог.
Живёт отдельно ровно потому, что четыре точки входа не должны четыре раза
описывать, как процесс переживает отсутствие секрета и как отвечает на SIGTERM.
"""

from __future__ import annotations

from sniffer.runtime.logs import setup_logging
from sniffer.runtime.service import Service, idle_loop, run_service, serve, sleep_or_stop

__all__ = [
    "Service",
    "idle_loop",
    "run_service",
    "serve",
    "setup_logging",
    "sleep_or_stop",
]
