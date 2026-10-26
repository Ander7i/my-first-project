"""Подделки для тестов: HTTP-сессия, клиент TikTok и файлы MP4."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tiktok_manager.api import API_BASE, Tokens

OPEN_ID = "open-1"
ALL_SCOPES = "user.info.basic,user.info.stats,video.list,video.upload,video.publish"


def make_tokens(
    now: datetime,
    *,
    open_id: str = OPEN_ID,
    access: str = "access-1",
    refresh: str = "refresh-1",
    access_ttl: timedelta = timedelta(hours=24),
    refresh_ttl: timedelta = timedelta(days=365),
    scope: str = ALL_SCOPES,
) -> Tokens:
    return Tokens(
        open_id=open_id,
        access_token=access,
        access_expires_at=now + access_ttl,
        refresh_token=refresh,
        refresh_expires_at=now + refresh_ttl,
        scope=scope,
    )


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Отвечает заранее заданными ответами и запоминает запросы."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], list[Any]] = defaultdict(list)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def on(self, method: str, url: str, *responses: Any) -> None:
        self._routes[(method, _full(url))].extend(responses)

    def ok(self, method: str, url: str, data: Any) -> None:
        """Успешный ответ в формате TikTok API v2."""
        self.on(method, url, FakeResponse(200, {"data": data, "error": {"code": "ok"}}))

    def error(self, method: str, url: str, code: str, status: int = 400, message: str = "") -> None:
        payload = {"data": {}, "error": {"code": code, "message": message, "log_id": "log-1"}}
        self.on(method, url, FakeResponse(status, payload))

    def requests_to(self, url: str) -> list[dict[str, Any]]:
        return [kwargs for _, called, kwargs in self.calls if called == _full(url)]

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        queue = self._routes[(method, url)]
        if not queue:
            raise AssertionError(f"неожиданный запрос {method} {url}")
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("PUT", url, **kwargs)


def _full(url: str) -> str:
    return API_BASE + url if url.startswith("/") else url


class FakeClient:
    """Клиент TikTok без сети для тестов публикации и планировщика."""

    def __init__(self) -> None:
        self.creator: dict[str, Any] = {
            "creator_nickname": "Мой канал",
            "creator_username": "my_channel",
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
            "max_video_post_duration_sec": 600,
            "comment_disabled": False,
            "duet_disabled": True,
            "stitch_disabled": False,
        }
        self.user: dict[str, Any] = {"display_name": "Мой канал", "follower_count": 10}
        self.video_list: list[dict[str, Any]] = []
        self.statuses: list[Any] = []
        self.errors: dict[str, BaseException] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._publish_ids = 0

    def _maybe_fail(self, name: str) -> None:
        error = self.errors.get(name)
        if error is not None:
            raise error

    def user_info(self, open_id: str) -> dict[str, Any]:
        self.calls.append(("user_info", open_id))
        self._maybe_fail("user_info")
        return dict(self.user)

    def videos(self, open_id: str) -> list[dict[str, Any]]:
        self.calls.append(("videos", open_id))
        self._maybe_fail("videos")
        return list(self.video_list)

    def creator_info(self, open_id: str) -> dict[str, Any]:
        self.calls.append(("creator_info", open_id))
        self._maybe_fail("creator_info")
        return dict(self.creator)

    def init_post(
        self, open_id: str, mode: str, plan: Any, post_info: Any = None
    ) -> tuple[str, str]:
        self.calls.append(("init_post", mode, post_info))
        self._maybe_fail("init_post")
        self._publish_ids += 1
        return f"pub-{self._publish_ids}", "https://upload.example/video"

    def upload(self, upload_url: str, path: Path, plan: Any, mime: str) -> None:
        self.calls.append(("upload", upload_url, mime))
        self._maybe_fail("upload")

    def publish_status(self, open_id: str, publish_id: str) -> dict[str, Any]:
        self.calls.append(("publish_status", publish_id))
        self._maybe_fail("publish_status")
        status = self.statuses.pop(0)
        if isinstance(status, BaseException):
            raise status
        return status

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


# ---------------------------------------------------------------- MP4


def box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def mvhd(timescale: int, duration: int, *, version: int = 0) -> bytes:
    if version == 1:
        times = bytes(16) + timescale.to_bytes(4, "big") + duration.to_bytes(8, "big")
    else:
        times = bytes(8) + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big")
    # Версия, флаги, времена, а дальше 80 байт прочих полей заголовка.
    return box(b"mvhd", bytes([version, 0, 0, 0]) + times + bytes(80))


def mp4_bytes(seconds: float = 15.0, *, moov_last: bool = False, padding: int = 1000) -> bytes:
    ftyp = box(b"ftyp", b"isom" + bytes(4) + b"isommp41")
    moov = box(b"moov", mvhd(1000, round(seconds * 1000)))
    mdat = box(b"mdat", bytes(padding))
    return ftyp + (mdat + moov if moov_last else moov + mdat)


def write_mp4(path: Path, seconds: float = 15.0, **kwargs: Any) -> Path:
    path.write_bytes(mp4_bytes(seconds, **kwargs))
    return path
