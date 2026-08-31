"""Веб-интерфейс владельца: маршруты и границы доступа.

Каждый маршрут с данными начинается с `_require_owner`. Это не декоратор и не
зависимость FastAPI намеренно: явный первый вызов в теле функции видно глазами
при чтении диффа, а забытая зависимость в списке параметров — нет.

Ошибки наружу отдаются обобщёнными: подробности уходят в лог. Страница
показывает переписку клиентов, и текст исключения на ней — это подсказка о
внутреннем устройстве тому, кто её увидеть не должен.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Cookie, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from sniffer.config import get_settings
from sniffer.dashboard import auth, data, reauth, views
from sniffer.dashboard.html import error_page, esc, login_page

log = structlog.get_logger(__name__)

OwnerCookie = Annotated[str | None, Cookie(alias=auth.COOKIE_NAME)]
CallNext = Callable[[Request], Awaitable[Response]]

# Второй рубеж после экранирования. Экранирование — основной механизм, но оно
# отменяется одной забытой строкой, а CSP не отменяется ничем: даже прошедший
# скрипт не сможет ни выполниться, ни отправить переписку клиентов наружу.
#
# Что откуда: скрипт виджета — с telegram.org, кнопка виджета — iframe с
# oauth.telegram.org, аватарки — с t.me. Больше внешних источников на странице
# нет. `frame-ancestors 'none'` запрещает встраивать НАС; `form-action 'self'` —
# отправлять наши формы на чужой домен.
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' https://telegram.org",
        # 'unsafe-inline' только для стилей: они лежат в <style> самой страницы.
        # Для скриптов inline не разрешён — там он и опасен.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https://t.me https://telegram.org",
        "frame-src https://oauth.telegram.org",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Ссылки в карточках ведут на чужие сайты; адрес страницы владельца им
    # знать незачем.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # Страница с перепиской клиентов не должна лежать в кэше прокси или
    # оставаться в истории браузера после выхода.
    "Cache-Control": "no-store",
}


class Unauthorized(Exception):
    """Cookie нет или она недействительна — показываем только вход."""


def _require_owner(cookie: str | None) -> None:
    if not auth.is_owner_session(cookie):
        raise Unauthorized


def _html(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status)


def create_app() -> FastAPI:
    # Документацию OpenAPI выключаем: описывать наружу нечего, а /docs — это
    # ещё одна страница, которая обязана быть закрытой, и однажды не будет.
    app = FastAPI(title="SnifferBot dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _headers(request: Request, call_next: CallNext) -> Response:
        """Заголовки на КАЖДЫЙ ответ, включая страницы ошибок и редиректы.

        Middleware, а не установка в каждом хендлере: забытый хендлер — это
        страница без CSP, и заметить это по диффу невозможно.
        """
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(Unauthorized)
    async def _unauthorized(_request: Request, _exc: Unauthorized) -> HTMLResponse:
        # 401 с страницей входа: неавторизованный не видит ни строки данных.
        return _html(login_page(get_settings().bot_username), status=401)

    @app.exception_handler(auth.AuthError)
    async def _auth_failed(_request: Request, exc: auth.AuthError) -> HTMLResponse:
        log.warning("dashboard.auth_failed", status=exc.status, reason=exc.message)
        return _html(error_page(exc.status, exc.message), status=exc.status)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("dashboard.failed", path=request.url.path, kind=type(exc).__name__)
        return _html(error_page(500, "Внутренняя ошибка. Подробности в логе сервиса."), status=500)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        """Проверка деплоем по HTTP. Без данных и без авторизации.

        Отвечает только «процесс жив и настроен»: чего не хватает — именами из
        `.env`. Ни базы, ни секретов здесь не видно.
        """
        return {"status": "ok", "missing": auth.missing_settings()}

    @app.get("/", response_class=HTMLResponse)
    async def overview(sniffer_owner: OwnerCookie = None) -> HTMLResponse:
        _require_owner(sniffer_owner)
        return _html(views.overview_page(await data.overview()))

    @app.get("/auth/telegram", response_class=HTMLResponse)
    async def telegram_login(request: Request) -> Response:
        """Возврат от Telegram Login Widget. Подпись обязательна.

        Cookie ставится только после того, как подпись проверена И id совпал с
        владельцем: чужой аккаунт получает 403, а не пустую страницу.
        """
        auth.authorize_widget(dict(request.query_params))
        token, ttl = auth.issue_session()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME,
            token,
            max_age=ttl,
            httponly=True,
            # Secure: снаружи сюда ходят только через HTTPS (Cloudflare → nginx).
            secure=True,
            # Lax, а не None: cookie не уезжает с кросс-сайтовым POST, и это
            # уже само по себе половина защиты от CSRF на форме сессии.
            samesite="lax",
            path="/",
        )
        log.info("dashboard.owner_logged_in")
        return response

    @app.get("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    @app.get("/requests/{request_id}", response_class=HTMLResponse)
    async def request_detail(request_id: int, sniffer_owner: OwnerCookie = None) -> HTMLResponse:
        _require_owner(sniffer_owner)
        detail = await data.request_detail(request_id)
        if detail is None:
            return _html(error_page(404, "Такого запроса нет."), status=404)
        return _html(views.request_page(detail))

    @app.get("/users/{user_id}", response_class=HTMLResponse)
    async def user_detail(user_id: int, sniffer_owner: OwnerCookie = None) -> HTMLResponse:
        _require_owner(sniffer_owner)
        detail = await data.user_detail(user_id)
        if detail is None:
            return _html(error_page(404, "Такого клиента нет."), status=404)
        return _html(views.user_page(detail))

    @app.get("/session", response_class=HTMLResponse)
    async def session_view(sniffer_owner: OwnerCookie = None) -> HTMLResponse:
        _require_owner(sniffer_owner)
        return _html(await _session_page())

    @app.post("/session/start", response_class=HTMLResponse)
    async def session_start(
        phone: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form()] = "",
        sniffer_owner: OwnerCookie = None,
    ) -> HTMLResponse:
        _require_owner(sniffer_owner)
        _require_csrf(csrf)
        try:
            flow_id = await reauth.start(phone)
        except reauth.ReauthError as exc:
            return _html(await _session_page(note=_bad(str(exc))), status=400)
        return _html(views.code_form(flow_id, csrf=auth.issue_csrf()))

    @app.post("/session/verify", response_class=HTMLResponse)
    async def session_verify(
        flow_id: Annotated[str, Form()] = "",
        code: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form()] = "",
        sniffer_owner: OwnerCookie = None,
    ) -> HTMLResponse:
        """Код и пароль живут только внутри этого вызова: ни лога, ни ответа."""
        _require_owner(sniffer_owner)
        _require_csrf(csrf)
        try:
            phone = await reauth.verify(flow_id, code=code, password=password)
        except reauth.NeedsPassword:
            return _html(
                views.code_form(
                    flow_id,
                    csrf=auth.issue_csrf(),
                    password=True,
                    note="<p class='mute'>У аккаунта включён облачный пароль.</p>",
                )
            )
        except reauth.ReauthError as exc:
            return _html(
                views.code_form(
                    flow_id,
                    csrf=auth.issue_csrf(),
                    password=reauth.needs_password(flow_id),
                    note=_bad(str(exc)),
                ),
                status=400,
            )
        return _html(
            await _session_page(
                note=f"<p class='good'>Сессия сохранена для {esc(phone)}. "
                "Коллектор подхватит её сам на ближайшем перезапуске.</p>"
            )
        )

    return app


def _require_csrf(token: str) -> None:
    if not auth.is_valid_csrf(token):
        raise auth.AuthError(403, "форма устарела — откройте страницу заново")


async def _session_page(note: str = "") -> str:
    return views.session_page(
        await data.session_states(),
        csrf=auth.issue_csrf(),
        phone=get_settings().tg_phone,
        note=note,
    )


def _bad(message: str) -> str:
    return f"<p class='bad'>{esc(message)}</p>"
