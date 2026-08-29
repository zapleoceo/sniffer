"""Точка входа: `python -m sniffer.bot` из docker-compose."""

from __future__ import annotations

from sniffer.bot.app import SERVICE
from sniffer.runtime.service import run_service

if __name__ == "__main__":
    run_service(SERVICE)
