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
from sniffer.broker.pins import pinned_model
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
    tool_calls: list[dict[str, Any]] | None = None


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
        self._settings = settings
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
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        model: str | None = None,
    ) -> BrokerResult:
        """`model` закрепляет «провайдер/модель»; без него — цепочка брокера.

        Закрепление не гарантия: брокер идёт только по указанной модели и при
        её отказе (квота, 400 на неизвестное имя, тайм-аут) возвращает ошибку,
        а не цепочку. Поэтому бот не молчит: один повтор без закрепления.
        """
        result, _fell_back = await self._chat(
            messages,
            capability=capability,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
        )
        return result

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        capability: str,
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int,
        temperature: float,
        model: str | None,
    ) -> tuple[BrokerResult, bool]:
        """`chat` и признак «закреплённая модель отказала, ответ дан цепочкой».

        Признак нужен `structured`: платный повтор по цепочке в цепочке запросов один, и если
        его уже сделал `chat`, второй (из-за негодного ответа) был бы третьей отправкой.
        """
        fell_back = False
        if tools is not None and (not tools or response_format is not None):
            raise ValueError("tools must be nonempty and cannot accompany response_format")
        if tool_choice is not None and tools is None:
            raise ValueError("tool_choice requires tools")
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"

        if model:
            try:
                result = await self._run(capability, {**payload, "model": model})
            except BrokerCapError:
                # Дневной cap — свойство проекта, а не модели: повтор его не обойдёт.
                raise
            except BrokerError as exc:
                log.warning(
                    "broker.pinned_model_failed",
                    capability=capability,
                    model=model,
                    error=str(exc)[:200],
                )
                result = await self._run(capability, payload)
                fell_back = True
        else:
            result = await self._run(capability, payload)
        await self._account(capability, result)
        return result, fell_back

    async def _run(self, capability: str, payload: dict[str, Any]) -> BrokerResult:
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
        """Request a schema, then independently validate the paid response.

        Provider constraints do not prevent truncation or refusal. Never repair
        output. The only resubmit is ONE unpinned retry for the whole call: either `chat`
        fell back after a pinned failure, or the pinned model returned invalid output
        (logged), never both; a cap error is never retried.
        """
        check_schema(schema)
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }
        pin = pinned_model(self._settings, schema_name)
        result, fell_back = await self._chat(
            messages,
            capability=capability,
            max_tokens=max_tokens,
            temperature=0.1,
            model=pin,
            response_format=response_format,
        )
        try:
            return _validated(result, schema)
        except BrokerOutputError as exc:
            # Без закрепления, либо ответ и так дан цепочкой после отказа закреплённой
            # модели (повтор уже был): вторая платная отправка в одной цепочке запрещена.
            if pin is None or fell_back:
                raise
            # Закреплённая модель ответила, но ответ негоден (слабая модель
            # ломает схему). Цель бота: не молчать и не ошибаться, поэтому
            # ОДИН платный повтор по цепочке брокера. Первый ответ уже учтён
            # в chat() под фактической моделью; повтор учтётся под своей.
            log.warning(
                "broker.pinned_model_invalid",
                capability=capability,
                model=pin,
                served_model=result.model,
                reason=exc.reason,
                request_id=result.request_id,
            )
        retry = await self.chat(
            messages,
            capability=capability,
            max_tokens=max_tokens,
            temperature=0.1,
            response_format=response_format,
        )
        return _validated(retry, schema)

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
                    tool_calls=body.get("tool_calls"),
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


def _validated(result: BrokerResult, schema: dict[str, Any]) -> dict[str, Any]:
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


def _as_int(value: Any) -> int | None:
    """Число из ответа брокера. Провайдеры присылают то `12`, то `"12"`."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
