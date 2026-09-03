"""Клиент AIbroker.

Чат у брокера только асинхронный: submit возвращает 202 с job_id, ответ
забирается поллингом. Синхронный /v1/chat отдаёт 410 Gone — держать
соединение нельзя, потому что цепочка фолбэков может идти дольше любого
разумного read-timeout.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from sniffer.broker.contracts import UsageSink
from sniffer.broker.output import InvalidOutput, OutputReason, check_schema, parse_object
from sniffer.config import get_settings

log = structlog.get_logger(__name__)

# Брокер отдаёт ровно этот текст, когда исчерпан дневной cap проекта.
# Ошибка ТЕРМИНАЛЬНАЯ: ретраи не создают бюджет, ждать до полуночи UTC.
CAP_ERROR = "daily budget cap reached"


class BrokerError(RuntimeError):
    """Брокер не смог выполнить запрос."""


class BrokerCapError(BrokerError):
    """Дневной лимит проекта исчерпан. Не ретраить до 00:00 UTC."""


class BrokerOutputError(BrokerError):
    """Paid output rejected locally; safe diagnostics never contain model text."""

    def __init__(self, reason: OutputReason, result: BrokerResult) -> None:
        self.reason = reason
        self.request_id = result.request_id
        self.job_id = result.job_id
        self.provider = result.provider
        self.finish_reason = result.finish_reason
        super().__init__(
            f"structured output rejected: {reason}; provider={self.provider}; "
            f"request_id={self.request_id}; job_id={self.job_id}"
        )


@dataclass(slots=True)
class BrokerResult:
    """Ответ брокера целиком, вместе с учётными полями.

    `request_id` — идентификатор строки в `usage_log` брокера. Без него связать
    наш запрос с расходом можно только по времени, то есть неверно при
    параллельных запросах (docs/dashboard.md). Ради него результат и расширен:
    раньше он нёс только текст, провайдера и стоимость.
    """

    text: str
    provider: str | None = None
    cost_usd: float | None = None
    request_id: int | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int | None = None
    job_id: int | None = None
    finish_reason: str | None = None
    refusal: bool = False


class BrokerClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        usage: UsageSink | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = settings.broker_url.rstrip("/")
        self._key = settings.broker_project_key
        self._timeout_s = settings.broker_timeout_s
        self._client = client or httpx.AsyncClient(timeout=30.0)
        # Приёмник учёта внедряется и только внедряется. Раньше здесь стояло
        # «None означает учёт по умолчанию», и `default_usage_sink` брался
        # импортом из `broker.usage` — а тот тянет `db.engine` и репозитории.
        # То есть контракт обещал независимость от базы, а импорт клиента
        # тянул SQLAlchemy: `pytest` без Postgres, маленькая утилита, любой
        # процесс без БД платили за это на ровном месте. Кто хочет учёт —
        # передаёт приёмник (`broker/usage.py::default_usage_sink`).
        self._usage = usage

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
        result = await self._poll(job_id)
        await self._account(capability, result)
        return result

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
        """Request a schema, then independently validate the paid response.

        Provider constraints do not prevent truncation or refusal. Never repair
        output or silently resubmit a paid call; callers choose their fallback.
        """
        check_schema(schema)
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
        if result.refusal:
            raise BrokerOutputError("refusal", result)
        if result.finish_reason is not None and (
            not isinstance(result.finish_reason, str)
            or result.finish_reason.lower() not in {"stop", "end_turn", "completed"}
        ):
            raise BrokerOutputError("incomplete", result)
        try:
            return parse_object(result.text, schema)
        except InvalidOutput as exc:
            # Do not chain jsonschema's exception: it contains the raw instance.
            raise BrokerOutputError(exc.reason, result) from None

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        """Голос → текст. Синхронный запрос: у брокера это прокси, не очередь.

        Отдельным методом, а не через `chat`, потому что это другой протокол:
        multipart с файлом вместо JSON с сообщениями, и свой эндпоинт
        `/v1/transcribe`. Общего у них только ключ проекта и учёт расходов.

        Ответ отдаёт `request_id` из `usage_log` брокера — тот же якорь, по
        которому связываются расходы обычных вызовов (docs/dashboard.md).
        """
        response = await self._client.post(
            f"{self._base_url}/v1/transcribe",
            headers={"X-Project-Key": self._key},
            files={"file": (filename, audio, "application/octet-stream")},
            timeout=self._timeout_s,
        )
        if response.status_code >= 400:
            raise BrokerError(f"transcribe {response.status_code}: {response.text[:200]}")
        body = response.json()
        await self._account(
            "transcription",
            BrokerResult(
                text="",
                provider=body.get("provider"),
                cost_usd=body.get("cost_usd"),
                request_id=_as_int(body.get("request_id")),
                model=body.get("model"),
                latency_ms=_as_int(body.get("latency_ms")),
            ),
        )
        return str(body.get("text") or "").strip()

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
                    request_id=_as_int(body.get("request_id")),
                    model=body.get("model"),
                    tokens_in=_as_int(body.get("tokens_in")) or 0,
                    tokens_out=_as_int(body.get("tokens_out")) or 0,
                    latency_ms=_as_int(body.get("latency_ms")),
                    job_id=job_id,
                    finish_reason=body.get("finish_reason"),
                    refusal=bool(body.get("refusal")),
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

    async def _account(self, capability: str, result: BrokerResult) -> None:
        """Записать расход. Ошибка учёта не отменяет полученный ответ.

        Учёт — служебная запись, а ответ модели уже оплачен: падать здесь
        значило бы выбрасывать то, за что заплатили, из-за недоступной базы.
        Поэтому ошибка идёт в лог, а не наружу.
        """
        if self._usage is None:
            return
        try:
            await self._usage(capability, result)
        # Широкий except намеренно: см. докстринг — ответ уже оплачен.
        except Exception as exc:
            log.warning("broker.usage_not_recorded", kind=type(exc).__name__, error=str(exc))


def _as_int(value: Any) -> int | None:
    """Число из ответа брокера. Провайдеры присылают то `12`, то `"12"`."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
