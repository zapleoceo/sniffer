"""Узкая DB-граница архивного источника.

Сам адаптер видит каталог, а не SQLAlchemy. Так его можно проверять без
Postgres, а вся сборка сессии и репозитория остаётся в одном месте.
"""

from __future__ import annotations

from typing import Any, Protocol

from sniffer.domain.records import Listing


class ListingCatalog(Protocol):
    async def search(self, params: dict[str, Any], *, limit: int) -> list[Listing]: ...


class RepositoryListingCatalog:
    async def search(self, params: dict[str, Any], *, limit: int) -> list[Listing]:
        from sniffer.sources.chat_directory import search_listings

        return await search_listings(params, limit=limit)


def new_catalog() -> ListingCatalog:
    return RepositoryListingCatalog()
