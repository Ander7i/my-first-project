"""Вход в TikTok по OAuth 2.0 для десктопных приложений (с PKCE)."""

from __future__ import annotations

import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .api import TikTokClient
from .config import DIRECT, INBOX, Config
from .db import Database

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"

# Статистика нужна всегда, право на публикацию — под выбранный режим.
BASE_SCOPES = ("user.info.basic", "user.info.stats", "video.list")
PUBLISH_SCOPES = {DIRECT: "video.publish", INBOX: "video.upload"}

LOGIN_TIMEOUT = 300

_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>TikTok</title></head>
<body style="font: 18px/1.5 sans-serif; margin: 40px">
<p>Готово, ответ TikTok получен. Вернитесь в консоль — вкладку можно закрыть.</p>
</body></html>
"""


class AuthError(RuntimeError):
    """Вход не удался."""


@dataclass(frozen=True)
class LoginResult:
    open_id: str
    display_name: str
    missing_scopes: tuple[str, ...]


def scopes_for(mode: str) -> tuple[str, ...]:
    return (*BASE_SCOPES, PUBLISH_SCOPES[mode])


def make_verifier() -> str:
    """Случайная строка для PKCE: 86 символов из A-Z, a-z, 0-9, «-» и «_»."""
    return secrets.token_urlsafe(64)


def make_challenge(verifier: str) -> str:
    """Хеш verifier'а для запроса входа.

    Не base64url, как в RFC 7636, а SHA-256 в hex: так требует документация
    TikTok Login Kit for Desktop.
    """
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


def authorize_url(config: Config, *, state: str, challenge: str, scopes: Sequence[str]) -> str:
    query = urlencode(
        {
            "client_key": config.client_key,
            "response_type": "code",
            "scope": ",".join(scopes),
            "redirect_uri": config.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def parse_callback(url: str, *, state: str) -> str:
    """Достаёт код авторизации из адреса, на который TikTok вернул браузер."""
    url = url.strip()
    query = parse_qs(urlsplit(url).query if "?" in url else url)

    def first(name: str) -> str:
        values = query.get(name)
        return values[0] if values else ""

    if first("error"):
        reason = first("error_description") or first("error")
        raise AuthError(f"TikTok не выполнил вход: {reason}")
    if first("state") != state:
        raise AuthError(
            "Ответ относится к другому запросу входа. Запустите login заново и "
            "пройдите по новой ссылке."
        )
    code = first("code")
    if not code:
        raise AuthError("В адресе нет кода входа. Скопируйте адрес из браузера целиком.")
    return code


class CallbackServer:
    """Локальный сервер на адресе возврата: на него TikTok отправляет браузер после входа."""

    def __init__(self, redirect_uri: str) -> None:
        parts = urlsplit(redirect_uri)
        self._path = (parts.path or "/").rstrip("/")
        self._received: list[str] = []
        received, expected = self._received, self._path

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # имя метода задаёт http.server
                if urlsplit(self.path).path.rstrip("/") != expected:
                    self.send_error(404)
                    return
                received.append(self.path)
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass  # без строк access-лога в консоли

        # Браузер приходит на localhost, а Python может повесить его на IPv6.
        host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname or "127.0.0.1"
        try:
            self._server = HTTPServer((host, parts.port or 80), Handler)
        except OSError as exc:
            raise AuthError(
                f"Не удалось занять порт {parts.port} для входа: {exc}. Закройте программу, "
                "которая его держит, или войдите с флагом --paste."
            ) from None
        self._server.timeout = 1

    def wait(self, timeout: float = LOGIN_TIMEOUT) -> str:
        """Ждёт, пока браузер придёт на адрес возврата. Возвращает путь с параметрами."""
        deadline = time.monotonic() + timeout
        while not self._received:
            if time.monotonic() > deadline:
                raise AuthError(
                    f"Вход не завершён за {int(timeout // 60)} мин. Запустите login ещё раз."
                )
            self._server.handle_request()
        return self._received[0]

    def close(self) -> None:
        self._server.server_close()

    def __enter__(self) -> CallbackServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def login(
    config: Config,
    client: TikTokClient,
    db: Database,
    *,
    paste: bool = False,
    open_browser: Callable[[str], bool] | None = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], object] = print,
) -> LoginResult:
    """Проводит вход: ссылка → согласие в браузере → код → токены в базе."""
    open_browser = open_browser or webbrowser.open
    config.require_app()
    verifier = make_verifier()
    state = secrets.token_urlsafe(16)
    scopes = scopes_for(config.publish_mode)
    url = authorize_url(config, state=state, challenge=make_challenge(verifier), scopes=scopes)

    if paste:
        say("Откройте ссылку в браузере на любом устройстве и войдите в TikTok:\n")
        say(url)
        say(
            "\nПосле входа браузер перейдёт на адрес localhost и, скорее всего, покажет "
            "ошибку — так и должно быть. Скопируйте этот адрес из строки браузера целиком."
        )
        callback = ask("Адрес: ")
    else:
        with CallbackServer(config.redirect_uri) as server:
            say("Открываю браузер для входа в TikTok. Если он не открылся, перейдите по ссылке:\n")
            say(url)
            if not open_browser(url):
                # Браузера нет — скорее всего, это сервер. Ссылку, открытую на
                # другом устройстве, TikTok вернёт на localhost того устройства.
                say(
                    "\nБраузер не открылся. Если вы на сервере без экрана, прервите вход "
                    "(Ctrl+C) и запустите: python -m tiktok_manager login --paste"
                )
            say("\nЖду ответа от TikTok...")
            callback = server.wait()

    code = parse_callback(callback, state=state)
    tokens = client.exchange_code(code, verifier)
    db.save_tokens(tokens)
    user = client.user_info(tokens.open_id)
    name = str(user.get("display_name") or "")
    db.set_display_name(tokens.open_id, name)
    missing = tuple(scope for scope in scopes if scope not in tokens.scopes)
    return LoginResult(open_id=tokens.open_id, display_name=name, missing_scopes=missing)
