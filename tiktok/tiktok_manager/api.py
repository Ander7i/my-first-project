"""Клиент официального TikTok API v2: токены, статистика и публикация роликов."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .config import DIRECT, INBOX, Config
from .db import Database, from_iso, utcnow
from .video import ChunkPlan

log = logging.getLogger(__name__)

API_BASE = "https://open.tiktokapis.com"
TOKEN_PATH = "/v2/oauth/token/"
INIT_PATHS = {
    DIRECT: "/v2/post/publish/video/init/",
    INBOX: "/v2/post/publish/inbox/video/init/",
}

USER_FIELDS = ("open_id", "display_name")
# Эти поля отдаются только с правом user.info.stats. Если попросить их без
# права, TikTok отклонит весь запрос, а не только лишние поля.
USER_STATS_FIELDS = ("follower_count", "following_count", "likes_count", "video_count")
VIDEO_FIELDS = (
    "id",
    "title",
    "video_description",
    "create_time",
    "share_url",
    "duration",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
)

# Токен обновляем заранее, чтобы он не истёк посреди загрузки ролика.
REFRESH_MARGIN = timedelta(minutes=10)
TIMEOUT = (10, 60)
UPLOAD_TIMEOUT = (10, 300)
UPLOAD_ATTEMPTS = 3
# Предохранитель: если API вдруг зациклит курсор, список роликов не будет бесконечным.
MAX_VIDEO_PAGES = 100

# Ошибки, которые проходят сами, — запрос стоит повторить позже.
RETRYABLE = {"rate_limit_exceeded", "internal_error", "network_error"}

RELOGIN = "Войдите заново: python -m tiktok_manager login"

HINTS = {
    "login_required": f"Доступ к аккаунту истёк или отозван. {RELOGIN}",
    "access_token_invalid": f"TikTok не принял токен. {RELOGIN}",
    "scope_not_authorized": (
        "Нет нужного разрешения. Проверьте, что право есть в Scopes приложения, "
        f"и отметьте все пункты на экране входа. {RELOGIN}"
    ),
    "scope_permission_missed": f"В токене не хватает разрешений. {RELOGIN}",
    "rate_limit_exceeded": "Слишком много запросов к TikTok, повторю позже.",
    "internal_error": "Временный сбой на стороне TikTok, повторю позже.",
    "network_error": "Нет связи с TikTok, повторю позже.",
    "invalid_param": "TikTok отклонил параметры запроса.",
    "invalid_params": "TikTok отклонил параметры запроса.",
    "spam_risk_too_many_posts": "Аккаунт исчерпал суточный лимит публикаций через API.",
    "spam_risk_user_banned_from_posting": "TikTok запретил этому аккаунту публиковать ролики.",
    "spam_risk_too_many_pending_share": (
        "Во «Входящих» TikTok уже 5 неопубликованных черновиков за сутки. "
        "Опубликуйте или удалите их в приложении."
    ),
    "reached_active_user_cap": "Приложение исчерпало суточную квоту публикующих аккаунтов.",
    "unaudited_client_can_only_post_to_private_accounts": (
        "Приложение не прошло аудит TikTok, поэтому публиковать может только в закрытый "
        "аккаунт и только с видимостью SELF_ONLY. Пройдите аудит или переключитесь на "
        "PUBLISH_MODE=inbox."
    ),
    "privacy_level_option_mismatch": "Такая видимость недоступна аккаунту.",
    "invalid_publish_id": "TikTok не знает такой публикации.",
    "upload_400": "TikTok отклонил часть файла.",
    "upload_403": "Ссылка для загрузки истекла — она действует час.",
    "upload_404": "TikTok не нашёл загрузку.",
    "upload_416": "TikTok ждал другую часть файла.",
}


class TikTokError(RuntimeError):
    """Ошибка TikTok API с объяснением по-русски."""

    def __init__(
        self, code: str, detail: str = "", *, status: int | None = None, log_id: str = ""
    ) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        self.log_id = log_id
        text = HINTS.get(code) or f"TikTok вернул ошибку {code}."
        if detail:
            # Текст от TikTok — по-английски, но помогает понять, что не так.
            text += f" ({detail})"
        super().__init__(text)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE or (self.status is not None and self.status >= 500)


class LoginRequired(TikTokError):
    """Токен не обновить — без нового входа к аккаунту не попасть."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("login_required", detail)


@dataclass(frozen=True)
class Tokens:
    open_id: str
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    scope: str

    @property
    def scopes(self) -> set[str]:
        return {part.strip() for part in self.scope.split(",") if part.strip()}

    @classmethod
    def from_response(
        cls, payload: Mapping[str, Any], now: datetime, *, open_id: str = ""
    ) -> Tokens:
        try:
            return cls(
                open_id=str(payload.get("open_id") or open_id),
                access_token=str(payload["access_token"]),
                access_expires_at=now + timedelta(seconds=int(payload.get("expires_in") or 0)),
                refresh_token=str(payload.get("refresh_token") or ""),
                refresh_expires_at=now
                + timedelta(seconds=int(payload.get("refresh_expires_in") or 0)),
                scope=str(payload.get("scope") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TikTokError("bad_response", f"неожиданный ответ с токеном: {exc}") from None


class TikTokClient:
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        session: Any = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.db = db
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep

    # ----------------------------------------------------------------- tokens

    def exchange_code(self, code: str, verifier: str) -> Tokens:
        """Меняет код из адреса возврата на токены доступа."""
        tokens = self._token_request(
            {
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.config.redirect_uri,
                "code_verifier": verifier,
            }
        )
        if not tokens.open_id or not tokens.refresh_token:
            raise TikTokError("bad_response", "TikTok не вернул open_id или refresh_token")
        return tokens

    def access_token(self, open_id: str, *, force_refresh: bool = False) -> str:
        """Действующий токен аккаунта; при необходимости обновляет его."""
        account = self.db.account(open_id)
        if account is None:
            raise LoginRequired("аккаунт не подключён")
        now = self.clock()
        if not force_refresh and from_iso(account["access_expires_at"]) - REFRESH_MARGIN > now:
            return str(account["access_token"])
        if from_iso(account["refresh_expires_at"]) <= now:
            raise LoginRequired("срок refresh-токена истёк")
        try:
            tokens = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": account["refresh_token"]},
                open_id=open_id,
            )
        except TikTokError as exc:
            if exc.retryable:
                raise
            raise LoginRequired(exc.detail or exc.code) from None
        if not tokens.refresh_token:
            tokens = replace(
                tokens,
                refresh_token=account["refresh_token"],
                refresh_expires_at=from_iso(account["refresh_expires_at"]),
            )
        # TikTok может выдать новый refresh-токен вместо старого — сохраняем всегда.
        self.db.save_tokens(tokens)
        return tokens.access_token

    def _token_request(self, form: Mapping[str, str], *, open_id: str = "") -> Tokens:
        data = {
            "client_key": self.config.client_key,
            "client_secret": self.config.client_secret,
            **form,
        }
        try:
            response = self.session.post(
                API_BASE + TOKEN_PATH,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise TikTokError("network_error", str(exc)) from None
        payload = _json(response)
        if response.status_code < 400 and payload.get("access_token"):
            return Tokens.from_response(payload, self.clock(), open_id=open_id)
        error = payload.get("error")
        if isinstance(error, dict):
            code, detail, log_id = _extract_error(payload)
        else:
            code = str(error or "")
            detail = str(payload.get("error_description") or "")
            log_id = str(payload.get("log_id") or "")
        raise TikTokError(
            code or f"http_{response.status_code}",
            detail,
            status=response.status_code,
            log_id=log_id,
        )

    # ------------------------------------------------------------- statistics

    def user_info(self, open_id: str) -> dict[str, Any]:
        account = self.db.account(open_id)
        scopes = set(account["scope"].split(",")) if account is not None else set()
        fields = USER_FIELDS + (USER_STATS_FIELDS if "user.info.stats" in scopes else ())
        data = self._call(open_id, "GET", "/v2/user/info/", fields=fields)
        user = data.get("user")
        return user if isinstance(user, dict) else {}

    def videos(self, open_id: str) -> list[dict[str, Any]]:
        """Все публичные ролики аккаунта, от новых к старым."""
        found: dict[str, dict[str, Any]] = {}
        cursor: Any = None
        for _ in range(MAX_VIDEO_PAGES):
            body: dict[str, Any] = {"max_count": 20}
            if cursor is not None:
                body["cursor"] = cursor
            data = self._call(open_id, "POST", "/v2/video/list/", fields=VIDEO_FIELDS, body=body)
            page = [v for v in data.get("videos") or [] if isinstance(v, dict) and v.get("id")]
            for video in page:
                found[str(video["id"])] = video
            next_cursor = data.get("cursor")
            if not data.get("has_more") or not page or next_cursor in (None, cursor):
                break
            cursor = next_cursor
        return list(found.values())

    # ------------------------------------------------------------- publishing

    def creator_info(self, open_id: str) -> dict[str, Any]:
        """Настройки аккаунта для публикации: доступная видимость, лимит длительности."""
        return self._call(open_id, "POST", "/v2/post/publish/creator_info/query/")

    def init_post(
        self,
        open_id: str,
        mode: str,
        plan: ChunkPlan,
        post_info: Mapping[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Открывает публикацию в TikTok. Возвращает publish_id и адрес для загрузки."""
        body: dict[str, Any] = {"source_info": plan.source_info()}
        if mode == DIRECT:
            body["post_info"] = dict(post_info or {})
        data = self._call(open_id, "POST", INIT_PATHS[mode], body=body)
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise TikTokError("bad_response", "TikTok не вернул publish_id или upload_url")
        return publish_id, upload_url

    def upload(self, upload_url: str, path: Path, plan: ChunkPlan, mime: str) -> None:
        """Загружает файл частями по порядку, как требует TikTok."""
        with open(path, "rb") as file:
            for first, last in plan.ranges():
                file.seek(first)
                chunk = file.read(last - first + 1)
                if len(chunk) != last - first + 1:
                    raise TikTokError("file_changed", "файл изменился во время загрузки")
                self._put_chunk(upload_url, chunk, f"bytes {first}-{last}/{plan.size}", mime)

    def publish_status(self, open_id: str, publish_id: str) -> dict[str, Any]:
        return self._call(
            open_id, "POST", "/v2/post/publish/status/fetch/", body={"publish_id": publish_id}
        )

    # ------------------------------------------------------------------- http

    def _call(
        self,
        open_id: str,
        method: str,
        path: str,
        *,
        fields: Sequence[str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload, status = self._send(open_id, method, path, fields, body, force_refresh=False)
        if _code(payload, status) == "access_token_invalid":
            # Токен мог стать недействительным раньше срока — обновляем и пробуем ещё раз.
            payload, status = self._send(open_id, method, path, fields, body, force_refresh=True)
        code = _code(payload, status)
        if code != "ok":
            _, detail, log_id = _extract_error(payload)
            raise TikTokError(code, detail, status=status, log_id=log_id)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _send(
        self,
        open_id: str,
        method: str,
        path: str,
        fields: Sequence[str] | None,
        body: Mapping[str, Any] | None,
        *,
        force_refresh: bool,
    ) -> tuple[dict[str, Any], int]:
        token = self.access_token(open_id, force_refresh=force_refresh)
        try:
            response = self.session.request(
                method,
                API_BASE + path,
                params={"fields": ",".join(fields)} if fields else None,
                json=dict(body) if body is not None else None,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise TikTokError("network_error", str(exc)) from None
        return _json(response), response.status_code

    def _put_chunk(self, url: str, chunk: bytes, content_range: str, mime: str) -> None:
        headers = {
            "Content-Type": mime,
            "Content-Length": str(len(chunk)),
            "Content-Range": content_range,
        }
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                response = self.session.put(
                    url, data=chunk, headers=headers, timeout=UPLOAD_TIMEOUT
                )
            except requests.RequestException as exc:
                if attempt == UPLOAD_ATTEMPTS:
                    raise TikTokError("network_error", str(exc)) from None
                log.warning("Сбой сети при загрузке (%s), попытка %s", exc, attempt)
            else:
                # 206 — часть принята, ждём следующую; 201 — файл загружен целиком.
                if response.status_code in (200, 201, 206):
                    return
                if response.status_code < 500 or attempt == UPLOAD_ATTEMPTS:
                    raise TikTokError(
                        f"upload_{response.status_code}",
                        response.text[:200],
                        status=response.status_code,
                    )
                log.warning(
                    "TikTok ответил %s на загрузку, попытка %s", response.status_code, attempt
                )
            self.sleep(2**attempt)


def _json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_error(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    """Код, текст и log_id ошибки. TikTok кладёт их то в error, то на верхний уровень."""
    error = payload.get("error")
    source = error if isinstance(error, dict) else payload
    return (
        str(source.get("code") or ""),
        str(source.get("message") or ""),
        str(source.get("log_id") or ""),
    )


def _code(payload: Mapping[str, Any], status: int) -> str:
    code, _, _ = _extract_error(payload)
    if code and code != "ok":
        return code
    if status >= 400:
        return f"http_{status}"
    return "ok"
