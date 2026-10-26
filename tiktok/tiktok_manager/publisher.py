"""Публикация роликов из очереди и отслеживание их статуса в TikTok."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from sqlite3 import Row
from typing import Any

from . import db as store
from .api import LoginRequired, TikTokClient, TikTokError
from .config import DIRECT, Config
from .db import Database, from_iso
from .planner import find_slot
from .slots import NoFreeSlot
from .video import VideoError, VideoFile, inspect_video, plan_chunks

log = logging.getLogger(__name__)

CAPTION_LIMIT = 2200
RETRY_DELAY = timedelta(minutes=5)
MAX_ATTEMPTS = 3
# Ссылка на загрузку живёт час. Если файл за это время не дошёл целиком,
# публикация уже не состоится — ролик можно отправить заново без риска дубля.
UPLOAD_EXPIRY = timedelta(minutes=70)
# Дольше этого итога обработки не ждём.
PROCESSING_TIMEOUT = timedelta(hours=24)
# Ролик взят в работу, но запрос в TikTok так и не ушёл — процесс упал.
STALE_CLAIM = timedelta(minutes=10)

FAIL_REASONS = {
    "file_format_check_failed": "TikTok не принял формат: нужен MP4 (H.264), MOV или WebM.",
    "duration_check_failed": "Длительность ролика не подходит этому аккаунту.",
    "frame_rate_check_failed": "Частота кадров должна быть от 23 до 60 FPS.",
    "picture_size_check_failed": "Стороны кадра должны быть от 360 до 4096 пикселей.",
    "internal": "Временный сбой TikTok.",
    "auth_removed": "Доступ приложения к аккаунту отозван. Войдите заново: "
    "python -m tiktok_manager login",
    "spam_risk_user_banned_from_posting": "TikTok запретил аккаунту публиковать ролики.",
    "spam_risk_text": "TikTok счёл подпись спамом. Измените текст и добавьте ролик заново.",
    "spam_risk": "TikTok счёл публикацию рискованной и отклонил её.",
    "spam_risk_too_many_posts": "Исчерпан суточный лимит публикаций через API. "
    "Повторите завтра командой retry.",
    "publish_cancelled": "Публикация отменена.",
}
# После таких отказов ролик не опубликован, и его можно спокойно отправить ещё раз.
RETRYABLE_FAIL_REASONS = {"internal"}


class PublishError(RuntimeError):
    """Публикация невозможна, пока человек что-то не поправит."""


def caption_length(text: str) -> int:
    """Длина подписи так, как её считает TikTok: в единицах UTF-16."""
    return len(text.encode("utf-16-le")) // 2


def explain_fail_reason(reason: str) -> str:
    return FAIL_REASONS.get(reason) or (
        f"TikTok отклонил публикацию: {reason or 'причина не указана'}."
    )


def check_creator(video: VideoFile, privacy_level: str, creator: Mapping[str, Any]) -> None:
    """Сверяет ролик с текущими настройками аккаунта в TikTok."""
    limit = creator.get("max_video_post_duration_sec")
    if video.duration and limit and video.duration > float(limit):
        raise PublishError(
            f"Ролик длится {video.duration:.0f} с, а этому аккаунту можно публиковать "
            f"не длиннее {limit} с."
        )
    options = [str(option) for option in creator.get("privacy_level_options") or []]
    if privacy_level not in options:
        raise PublishError(
            f"Видимость {privacy_level} сейчас недоступна аккаунту. "
            f"Доступны: {', '.join(options) or 'никакие'}."
        )


def build_post_info(item: Row, creator: Mapping[str, Any]) -> dict[str, Any]:
    """Параметры публикации в режиме direct."""
    return {
        "title": item["caption"],
        "privacy_level": item["privacy_level"],
        # Что выключено в настройках аккаунта, через API не включить.
        "disable_comment": not item["allow_comment"] or bool(creator.get("comment_disabled")),
        "disable_duet": not item["allow_duet"] or bool(creator.get("duet_disabled")),
        "disable_stitch": not item["allow_stitch"] or bool(creator.get("stitch_disabled")),
        "brand_organic_toggle": bool(item["brand_organic"]),
        "brand_content_toggle": bool(item["brand_content"]),
        "is_aigc": bool(item["is_aigc"]),
    }


def publish(item: Row, *, client: TikTokClient, db: Database, now: datetime) -> None:
    """Отправляет ролик в TikTok. Чем кончилось, потом выясняет check_status."""
    item_id = item["id"]
    if not db.claim(item_id, now):
        return  # ролик уже забрал другой процесс
    log.info("#%s: отправляю %s в TikTok (режим %s)", item_id, item["path"], item["mode"])
    open_id = item["open_id"]
    try:
        video = inspect_video(item["path"])
        plan = plan_chunks(video.size)
        post_info = None
        if item["mode"] == DIRECT:
            creator = client.creator_info(open_id)
            check_creator(video, item["privacy_level"], creator)
            post_info = build_post_info(item, creator)
        publish_id, upload_url = client.init_post(open_id, item["mode"], plan, post_info)
    except LoginRequired:
        db.unclaim(item_id, now)
        raise
    except TikTokError as exc:
        if exc.retryable:
            _retry_later(db, item, now, str(exc))
        else:
            _fail(db, item_id, str(exc), now)
        return
    except (VideoError, PublishError) as exc:
        _fail(db, item_id, str(exc), now)
        return

    db.set_publish_id(item_id, publish_id, now)
    try:
        client.upload(upload_url, video.path, plan, video.mime)
    except (TikTokError, OSError) as exc:
        # Последняя часть могла дойти до TikTok, даже если ответ потерялся.
        # Повторять вслепую нельзя — выйдет дубль. Итог выяснит check_status.
        log.warning("#%s: загрузка прервалась (%s), уточню статус у TikTok", item_id, exc)
        return
    db.set_status(item_id, store.PROCESSING, now)
    log.info("#%s: файл загружен, TikTok обрабатывает ролик", item_id)


def check_status(
    item: Row, *, client: TikTokClient, db: Database, config: Config, now: datetime
) -> None:
    """Узнаёт у TikTok, чем кончилась отправленная публикация."""
    item_id = item["id"]
    if not item["publish_id"]:
        # Ролик взяли в работу, но запрос в TikTok так и не ушёл — процесс
        # упал. В TikTok ничего нет, поэтому просто возвращаем ролик в очередь.
        if now - from_iso(item["updated_at"]) > STALE_CLAIM:
            db.unclaim(item_id, now)
        return

    try:
        data = client.publish_status(item["open_id"], item["publish_id"])
    except LoginRequired:
        raise
    except TikTokError as exc:
        if exc.retryable:
            log.warning("#%s: статус не получен: %s", item_id, exc)
        else:
            _fail(db, item_id, str(exc), now)
        return

    status = str(data.get("status") or "")
    started = from_iso(item["started_at"] or item["updated_at"])
    if status == "PUBLISH_COMPLETE":
        # Поле и правда называется с опечаткой — так в API TikTok.
        post_ids = data.get("publicaly_available_post_id") or []
        post_id = str(post_ids[0]) if post_ids else ""
        db.set_status(item_id, store.PUBLISHED, now, post_id=post_id)
        log.info("#%s: опубликован", item_id)
    elif status == "SEND_TO_USER_INBOX":
        db.set_status(item_id, store.INBOX_SENT, now)
        log.info(
            "#%s: черновик во «Входящих» TikTok — откройте уведомление в приложении "
            "и опубликуйте. Подпись: %s",
            item_id,
            item["caption"] or "(без подписи)",
        )
    elif status == "FAILED":
        reason = str(data.get("fail_reason") or "")
        if reason in RETRYABLE_FAIL_REASONS:
            _retry_at_next_slot(config, db, item, now, explain_fail_reason(reason))
        else:
            _fail(db, item_id, explain_fail_reason(reason), now)
    elif item["status"] == store.UPLOADING and now - started > UPLOAD_EXPIRY:
        _retry_at_next_slot(config, db, item, now, "Загрузка файла не завершилась.")
    elif now - started > PROCESSING_TIMEOUT:
        _fail(db, item_id, "TikTok за сутки не сообщил итог. Проверьте профиль в приложении.", now)


def _fail(db: Database, item_id: int, reason: str, now: datetime) -> None:
    db.fail(item_id, reason, now)
    log.error("#%s: не опубликован. %s", item_id, reason)


def _retry_later(db: Database, item: Row, now: datetime, reason: str) -> None:
    attempts = item["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        _fail(db, item["id"], f"{reason} Не вышло за {MAX_ATTEMPTS} попытки.", now)
        return
    db.reschedule(item["id"], now + RETRY_DELAY, now, attempts=attempts, note=reason)
    log.warning("#%s: %s Повторю через %s мин.", item["id"], reason, RETRY_DELAY.seconds // 60)


def _retry_at_next_slot(
    config: Config, db: Database, item: Row, now: datetime, reason: str
) -> None:
    """Возвращает ролик в очередь на ближайший пик: исходное время уже прошло."""
    attempts = item["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        _fail(db, item["id"], f"{reason} Не вышло за {MAX_ATTEMPTS} попытки.", now)
        return
    try:
        slot, _ = find_slot(config, db, item["open_id"], now, exclude_id=item["id"])
    except NoFreeSlot as exc:
        _fail(db, item["id"], f"{reason} {exc}", now)
        return
    db.reschedule(item["id"], slot, now, attempts=attempts, note=reason)
    log.warning(
        "#%s: %s Перенёс на %s.",
        item["id"],
        reason,
        slot.astimezone(config.timezone).strftime("%d.%m %H:%M"),
    )
