"""Клиент AIbroker.

Чат у брокера только асинхронный: submit возвращает 202 с job_id, ответ
забирается поллингом. Синхронный /v1/chat отдаёт 410 Gone — держать
соединение нельзя, потому что цепочка фолбэков может идти дольше любого
разумного read-timeout.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from sniffer.config import get_settings

log = structlog.get_logger(__name__)

# Брокер отдаёт ровно этот текст, когда исчерпан дневной cap проекта.
# Ошибка ТЕРМИНАЛЬНАЯ: ретраи не создают бюджет, ждать до полуночи UTC.
CAP_ERROR = "daily budget cap reached"


class BrokerError(RuntimeError):
    """Брокер не смог выполнить запрос."""


class BrokerCapError(BrokerError):
    """Дневной лимит проекта исчерпан. Не ретраить до 00:00 UTC."""


@dataclass(slots=True)
class BrokerResult:
    text: str
    provider: str | None = None
    cost_usd: float | None = None


class BrokerClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._base_url = settings.broker_url.rstrip("/")
        self._key = settings.broker_project_key
        self._timeout_s = settings.broker_timeout_s
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        capability: str = "chat:fast",
        response_format: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> BrokerResult:
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        job_id = await self._submit(capability, payload)
        return await self._poll(job_id)

    async def structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        capability: str = "structured",
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Строгий JSON по схеме.

        Отправляем полный json_schema, а не bare json_object: провайдеры со
        схемной поддержкой грамматически ограничивают генерацию, и модель
        физически не может вернуть битый JSON. Это корневое лечение
        InvalidJSON, а не постфактум-валидация.
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        result = await self.chat(
            messages,
            capability=capability,
            max_tokens=max_tokens,
            temperature=0.1,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        )
        try:
            parsed: dict[str, Any] = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise BrokerError(f"провайдер {result.provider} вернул невалидный JSON") from exc
        return parsed

    async def _submit(self, capability: str, payload: dict[str, Any]) -> int:
        response = await self._client.post(
            f"{self._base_url}/v1/jobs",
            params={"capability": capability},
            headers={"X-Project-Key": self._key},
            json=payload,
        )
        if response.status_code >= 400:
            raise BrokerError(f"submit {response.status_code}: {response.text[:300]}")
        job_id: int = response.json()["job_id"]
        return job_id

    async def _poll(self, job_id: int) -> BrokerResult:
        deadline = time.monotonic() + self._timeout_s
        wait_s = 2.0

        while time.monotonic() < deadline:
            await asyncio.sleep(wait_s)
            response = await self._client.get(
                f"{self._base_url}/v1/jobs/{job_id}",
                headers={"X-Project-Key": self._key},
            )
            if response.status_code >= 400:
                raise BrokerError(f"poll {response.status_code}: {response.text[:300]}")

            body = response.json()
            status = body.get("status")

            if status == "done":
                return BrokerResult(
                    text=body.get("text", ""),
                    provider=body.get("provider"),
                    cost_usd=body.get("cost_usd"),
                )
            if status == "error":
                error = str(body.get("error", ""))
                if CAP_ERROR in error:
                    log.warning("broker.cap_reached", job_id=job_id)
                    raise BrokerCapError(error)
                raise BrokerError(error)

            # Брокер сам подсказывает, когда прийти снова, и расширяет
            # интервал для долгих задач — уважаем это вместо своего бэкоффа.
            wait_s = float(body.get("poll_after_s", wait_s))

        raise BrokerError(f"job {job_id} не завершился за {self._timeout_s}s")
