# ruff: noqa: S608 -- SQL fragments are fixed literals; all request values are bound.
"""Original public archive records selected through a structured legacy index."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from sniffer.db.repositories.base import Repository


class CollectionSourceRepository(Repository):
    async def archive(self, scope: dict[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
        """Return originals, while legacy listing facts only narrow candidates."""
        city, category, deal_type = (
            scope.get("city"),
            scope.get("category"),
            scope.get("deal_type"),
        )
        criteria = scope.get("criteria") or {}
        if (
            city not in {"nha_trang", "da_nang"}
            or not isinstance(category, str)
            or not isinstance(deal_type, str)
            or not isinstance(criteria, dict)
            or type(limit) is not int
            or not 1 <= limit <= 12
        ):
            raise ValueError("invalid_archive_scope")
        districts = _strings(criteria.get("districts"))
        must_have = _strings(criteria.get("must_have"))
        deal_breakers = _strings(criteria.get("deal_breakers"))
        attributes = {
            key: criteria[key]
            for key in ("brand", "model", "transmission", "rooms", "furnished")
            if criteria.get(key) is not None
        }
        clauses = [
            "c.is_active",
            "c.city=:city",
            "c.username ~ '^[A-Za-z0-9_]{5,32}$'",
            "r.text<>''",
            "r.posted_at>clock_timestamp()-interval '30 days'",
            "l.category=:category",
            "l.deal_type=:deal_type",
            "l.is_active",
        ]
        params: dict[str, Any] = {
            "city": city,
            "category": category,
            "deal_type": deal_type,
            "limit": limit,
        }
        for index, (key, value) in enumerate(attributes.items()):
            # Unknown legacy values remain candidates; each known value is checked
            # independently so a missing second attribute cannot reject the row.
            key_name, value_name = f"attribute_key_{index}", f"attribute_value_{index}"
            clauses.append(
                f"(NOT l.attributes ? :{key_name} OR l.attributes @> CAST(:{value_name} AS jsonb))"
            )
            params[key_name] = key
            params[value_name] = json.dumps({key: value}, ensure_ascii=False)
        if districts:
            district_terms = [district.replace("_", " ") for district in districts]
            district_parts = []
            for index, district in enumerate(district_terms):
                name = f"district_{index}"
                district_parts.append(f"r.text ILIKE :{name}")
                params[name] = f"%{district}%"
            clauses.append(
                "(l.district = ANY(CAST(:districts AS text[])) OR "
                + " OR ".join(district_parts)
                + ")"
            )
            params["districts"] = districts
        _text_clauses(clauses, params, "must", must_have, negative=False)
        _text_clauses(clauses, params, "break", deal_breakers, negative=True)
        budget_min, budget_max = criteria.get("budget_min"), criteria.get("budget_max")
        currency = criteria.get("budget_currency")
        price_column = "l.price_amount" if currency == "VND" else "l.price_usd_month"
        if currency in {"VND", "USD"} and isinstance(budget_min, int):
            clauses.append(f"({price_column} IS NULL OR {price_column}>=:budget_min)")
            params["budget_min"] = budget_min
        if currency in {"VND", "USD"} and isinstance(budget_max, int):
            clauses.append(f"({price_column} IS NULL OR {price_column}<=:budget_max)")
            params["budget_max"] = budget_max
        engine_cc = criteria.get("engine_cc")
        if isinstance(engine_cc, int):
            direction = criteria.get("engine_cc_dir")
            known = "(l.attributes->>'engine_cc') ~ '^[0-9]+$'"
            value = "CAST(l.attributes->>'engine_cc' AS integer)"
            if direction == "min":
                clauses.append(f"({known} AND {value}>=:engine_cc)")
                params["engine_cc"] = engine_cc
            elif direction == "max":
                clauses.append(f"(NOT ({known}) OR {value}<=:engine_cc)")
                params["engine_cc"] = engine_cc
            else:
                clauses.append(f"(NOT ({known}) OR {value} BETWEEN :engine_low AND :engine_high)")
                params.update(engine_low=int(engine_cc * 0.75), engine_high=int(engine_cc * 1.25))
        # Every clause is an application-owned literal above; customer values
        # remain bound parameters. Only the min/max operator is chosen locally.
        statement = f"""  # noqa: S608
            SELECT r.chat_tg_id,r.msg_id,r.text,r.posted_at,r.ingested_at,c.username
            FROM raw_messages r
            JOIN chats c ON c.tg_id=r.chat_tg_id
            JOIN listings l ON l.raw_message_id=r.id
            WHERE {" AND ".join(clauses)}
            ORDER BY r.posted_at DESC,r.id DESC LIMIT :limit
        """
        result = await self._session.execute(text(statement), params)
        return [dict(row) for row in result.mappings()]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        return []
    return [item for item in value if isinstance(item, str) and 0 < len(item) <= 100]


def _text_clauses(
    clauses: list[str], params: dict[str, Any], prefix: str, values: list[str], *, negative: bool
) -> None:
    for index, value in enumerate(values):
        name = f"{prefix}_{index}"
        clauses.append(f"r.text {'NOT ILIKE' if negative else 'ILIKE'} :{name}")
        params[name] = f"%{value}%"
