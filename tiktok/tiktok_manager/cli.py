"""Командная строка: python -m tiktok_manager <команда>."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from sqlite3 import Row
from typing import Any

from . import db as store
from . import report
from .api import LoginRequired, TikTokClient, TikTokError
from .auth import PUBLISH_SCOPES, login
from .config import DIRECT, INBOX, Config, load_config
from .db import Database, PostSettings, from_iso, utcnow
from .planner import (
    PROFILE_DAYS,
    find_slot,
    free_slots,
    peak_hours,
    recent_gains,
    slot_rules,
    views_by_day,
)
from .publisher import CAPTION_LIMIT, caption_length, check_creator
from .runner import Runner, account_label, collect
from .slots import NoFreeSlot
from .video import MIME_TYPES, VideoFile, inspect_video

PRIVACY_LEVELS = (
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
)
# Что показывать в очереди без --all: всё незавершённое и итоги за неделю.
QUEUE_HISTORY = timedelta(days=7)

Handler = Callable[[argparse.Namespace, Config, Database], int]


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.command == "run" else logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    try:
        config = load_config()
        db = Database(config.db_path)
        try:
            handler: Handler = args.handler
            return handler(args, config, db)
        finally:
            db.close()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 130
    except RuntimeError as exc:
        # Понятные ошибки показываем без длинного traceback.
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tiktok_manager",
        description="Статистика TikTok-аккаунта и публикация роликов в пиковые часы.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="команда")

    def command(name: str, handler: Handler, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text, description=text)
        sub.set_defaults(handler=handler)
        return sub

    def with_account(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument(
            "-a", "--account", help="имя аккаунта или open_id, если подключено несколько"
        )
        return sub

    sub = command("login", cmd_login, "подключить аккаунт TikTok")
    sub.add_argument(
        "--paste",
        action="store_true",
        help="вход на сервере без браузера: ссылку открыть на другом устройстве, "
        "а адрес из браузера вставить сюда",
    )

    command("accounts", cmd_accounts, "подключённые аккаунты")

    with_account(command("collect", cmd_collect, "снять статистику прямо сейчас"))

    sub = with_account(command("stats", cmd_stats, "статистика аккаунта и роликов"))
    sub.add_argument(
        "--days", type=_positive, default=7, help="за сколько дней показывать изменения (7)"
    )
    sub.add_argument(
        "--top", type=_positive, default=10, help="сколько последних роликов показать (10)"
    )
    sub.add_argument("--update", action="store_true", help="сначала снять свежую статистику")

    with_account(command("peak", cmd_peak, "пиковые часы аудитории и ближайшие слоты"))

    sub = with_account(command("add", cmd_add, "поставить ролики в очередь на публикацию"))
    sub.add_argument("files", nargs="+", metavar="ФАЙЛ", help="видео MP4, MOV, WebM или папка")
    sub.add_argument(
        "-c",
        "--caption",
        help="подпись с хештегами; без неё берётся текст из файла <имя ролика>.txt рядом",
    )
    sub.add_argument("--at", help="своё время вместо пика: «19:00», «завтра 19:00», «26.10 19:00»")
    sub.add_argument(
        "--privacy",
        type=str.upper,
        choices=PRIVACY_LEVELS,
        help="кто увидит ролик; обязательно в режиме direct",
    )
    sub.add_argument("--allow-comments", action="store_true", help="разрешить комментарии")
    sub.add_argument("--allow-duet", action="store_true", help="разрешить дуэты")
    sub.add_argument("--allow-stitch", action="store_true", help="разрешить stitch")
    sub.add_argument("--promo", action="store_true", help="ролик продвигает ваш бизнес")
    sub.add_argument("--paid", action="store_true", help="оплаченная реклама чужого бренда")
    sub.add_argument("--ai", action="store_true", help="ролик создан с помощью ИИ")

    sub = with_account(command("queue", cmd_queue, "очередь публикаций"))
    sub.add_argument("--all", action="store_true", help="показать и давно завершённые")

    sub = command("remove", cmd_remove, "убрать ролик из очереди")
    sub.add_argument("id", type=int, help="номер в очереди")

    sub = command("retry", cmd_retry, "вернуть ролик с ошибкой в очередь на ближайший пик")
    sub.add_argument("id", type=int, help="номер в очереди")

    command(
        "run", cmd_run, "запустить планировщик: статистика раз в час и публикации по расписанию"
    )
    return parser


# ---------------------------------------------------------------- commands


def cmd_login(args: argparse.Namespace, config: Config, db: Database) -> int:
    config.require_app()
    result = login(config, TikTokClient(config, db), db, paste=args.paste)
    print(f"\nГотово: подключён аккаунт «{result.display_name or result.open_id}».")
    if result.missing_scopes:
        print(
            "Внимание: TikTok не выдал разрешения " + ", ".join(result.missing_scopes) + ". "
            "Без них часть функций работать не будет. Проверьте Scopes в настройках "
            "приложения и войдите снова, отметив все пункты."
        )
    print(
        "Дальше: python -m tiktok_manager collect — снять статистику, "
        "python -m tiktok_manager run — запустить планировщик."
    )
    return 0


def cmd_accounts(args: argparse.Namespace, config: Config, db: Database) -> int:
    accounts = db.accounts()
    if not accounts:
        print("Аккаунтов нет. Подключите: python -m tiktok_manager login")
        return 0
    now = utcnow()
    rows = []
    for account in accounts:
        expires = from_iso(account["refresh_expires_at"])
        access = (
            "нужно войти заново"
            if expires <= now
            else f"до {expires.astimezone(config.timezone):%d.%m.%Y}"
        )
        scopes = set(account["scope"].split(","))
        modes = [mode for mode, scope in PUBLISH_SCOPES.items() if scope in scopes]
        rows.append((account_label(account), account["open_id"], access, ", ".join(modes) or "—"))
    print(report.table(("Аккаунт", "open_id", "Доступ", "Публикация"), rows))
    return 0


def cmd_collect(args: argparse.Namespace, config: Config, db: Database) -> int:
    config.require_app()
    client = TikTokClient(config, db)
    now = utcnow()
    accounts = [pick_account(db, args.account)] if args.account else _all_accounts(db)
    for account in accounts:
        user, videos = collect(client, db, account["open_id"], now)
        views = sum(int(video.get("view_count") or 0) for video in videos)
        followers = user.get("follower_count")
        print(
            f"«{user.get('display_name') or account_label(account)}»: "
            + (f"подписчиков {report.num(followers)}, " if followers is not None else "")
            + f"публичных роликов {len(videos)}, просмотров у них {report.num(views)}."
        )
    return 0


def cmd_stats(args: argparse.Namespace, config: Config, db: Database) -> int:
    account = pick_account(db, args.account)
    open_id = account["open_id"]
    now = utcnow()
    if args.update:
        config.require_app()
        collect(TikTokClient(config, db), db, open_id, now)
    snapshot_at = db.last_snapshot_at(open_id)
    if snapshot_at is None:
        print(
            f"Статистики по «{account_label(account)}» пока нет. "
            "Снимите её: python -m tiktok_manager collect"
        )
        return 0
    print(
        report.format_stats(
            name=account_label(account),
            snapshot_at=snapshot_at,
            latest=db.latest_account_stats(open_id),
            baseline=db.account_stats_at(open_id, now - timedelta(days=args.days)),
            videos=db.videos(open_id),
            gains=recent_gains(db, open_id, now - timedelta(hours=24)),
            daily=views_by_day(config, db, open_id, now, days=args.days),
            top=args.top,
            tz=config.timezone,
            now=now,
        )
    )
    return 0


def cmd_peak(args: argparse.Namespace, config: Config, db: Database) -> int:
    account = pick_account(db, args.account)
    now = utcnow()
    peaks, profile = peak_hours(config, db, account["open_id"], now)
    try:
        slots = free_slots(config, db, account["open_id"], now, peaks, count=3)
    except NoFreeSlot as exc:
        slots = []
        print(exc)
    print(
        report.format_peak(
            profile,
            peaks,
            slots,
            rules=slot_rules(config),
            days=PROFILE_DAYS,
            tz=config.timezone,
            now=now,
        )
    )
    return 0


def cmd_add(args: argparse.Namespace, config: Config, db: Database) -> int:
    account = pick_account(db, args.account)
    open_id = account["open_id"]
    mode = config.publish_mode
    _require_publish_scope(account, mode)

    if mode == DIRECT:
        if not args.privacy:
            raise RuntimeError(
                "Укажите, кто увидит ролик: --privacy PUBLIC_TO_EVERYONE (все), "
                "MUTUAL_FOLLOW_FRIENDS (друзья), FOLLOWER_OF_CREATOR (подписчики) или "
                "SELF_ONLY (только вы). Значения по умолчанию нет — так требует TikTok."
            )
        if args.paid and args.privacy == "SELF_ONLY":
            raise RuntimeError(
                "Рекламу бренда (--paid) TikTok не разрешает публиковать с видимостью SELF_ONLY."
            )
    elif any(
        (
            args.privacy,
            args.allow_comments,
            args.allow_duet,
            args.allow_stitch,
            args.promo,
            args.paid,
            args.ai,
        )
    ):
        print(
            "Режим inbox: видимость, разрешения и пометки о рекламе выбираются в "
            "приложении TikTok, когда вы публикуете черновик. Здесь они не передаются."
        )

    # Сначала проверяем все файлы: лучше узнать об ошибке до того, как часть
    # роликов уже встала в очередь.
    videos = [inspect_video(path) for path in _expand_files(args.files)]
    captions = [_caption_for(video, args.caption) for video in videos]

    if mode == DIRECT:
        creator = _creator_preview(config, db, open_id)
        if creator is not None:
            nickname = creator.get("creator_nickname") or creator.get("creator_username")
            print(f"Ролики будут опубликованы в аккаунт «{nickname}».")
            for video in videos:
                check_creator(video, args.privacy, creator)

    now = utcnow()
    manual_time = None
    if args.at:
        if len(videos) > 1:
            raise RuntimeError(
                "--at задаёт одно время на один ролик. Добавьте ролики по одному "
                "или без --at — тогда время подберётся по пикам само."
            )
        manual_time = parse_when(args.at, now, config.timezone)

    for video, caption in zip(videos, captions, strict=True):
        if manual_time is not None:
            slot, reason = manual_time, "время задано вручную"
        else:
            slot, peaks = find_slot(config, db, open_id, now)
            reason = report.PEAK_SOURCES[peaks.source]
        item_id = db.add_item(
            open_id=open_id,
            path=str(video.path),
            mode=mode,
            settings=PostSettings(
                caption=caption,
                privacy_level=(args.privacy or "") if mode == DIRECT else "",
                allow_comment=args.allow_comments,
                allow_duet=args.allow_duet,
                allow_stitch=args.allow_stitch,
                brand_organic=args.promo,
                brand_content=args.paid,
                is_aigc=args.ai,
            ),
            publish_at=slot,
            auto_slot=manual_time is None,
        )
        print(
            f"#{item_id} {video.path.name} → {report.when(slot, config.timezone, now=now)} "
            f"({reason})"
        )

    print("\nРолики уйдут в TikTok, пока работает планировщик: python -m tiktok_manager run")
    if mode == INBOX:
        print(
            "Режим inbox: в нужное время ролик придёт черновиком во «Входящие» TikTok — "
            "откройте уведомление и опубликуйте. Подпись покажет команда queue."
        )
    return 0


def cmd_queue(args: argparse.Namespace, config: Config, db: Database) -> int:
    now = utcnow()
    if args.account:
        items = db.items(pick_account(db, args.account)["open_id"])
    else:
        items = db.items()
    if not args.all:
        items = [
            item
            for item in items
            if item["status"] in (store.SCHEDULED, *store.IN_FLIGHT)
            or from_iso(item["updated_at"]) >= now - QUEUE_HISTORY
        ]
    names = None
    if len({item["open_id"] for item in items}) > 1:
        names = {account["open_id"]: account_label(account) for account in db.accounts()}
    print(report.format_queue(items, config.timezone, now=now, names=names))
    return 0


def cmd_remove(args: argparse.Namespace, config: Config, db: Database) -> int:
    item = _item(db, args.id)
    if item["status"] in store.IN_FLIGHT:
        raise RuntimeError(f"#{args.id} уже отправляется в TikTok — убрать его нельзя.")
    db.delete_item(args.id)
    print(f"#{args.id} убран из очереди. Сам файл не тронут: {item['path']}")
    return 0


def cmd_retry(args: argparse.Namespace, config: Config, db: Database) -> int:
    item = _item(db, args.id)
    if item["status"] != store.FAILED:
        status = report.STATUS_LABELS.get(item["status"], item["status"])
        raise RuntimeError(f"#{args.id} не в ошибке, а «{status}» — повторять нечего.")
    now = utcnow()
    slot, peaks = find_slot(config, db, item["open_id"], now, exclude_id=item["id"])
    db.reschedule(item["id"], slot, now, attempts=0)
    print(
        f"#{args.id} снова в очереди: {report.when(slot, config.timezone, now=now)} "
        f"({report.PEAK_SOURCES[peaks.source]})"
    )
    return 0


def cmd_run(args: argparse.Namespace, config: Config, db: Database) -> int:
    config.require_app()
    _all_accounts(db)
    Runner(config, db, TikTokClient(config, db)).run_forever()
    return 0


# ----------------------------------------------------------------- helpers


def pick_account(db: Database, wanted: str | None) -> Row:
    """Аккаунт по имени или open_id; без имени — единственный подключённый."""
    accounts = _all_accounts(db)
    if wanted:
        value = wanted.strip()
        name = value.lstrip("@").casefold()
        for account in accounts:
            if account["open_id"] == value or account["display_name"].casefold() == name:
                return account
        raise RuntimeError(f"Аккаунт {wanted!r} не найден. Подключены: {_names(accounts)}.")
    if len(accounts) > 1:
        raise RuntimeError(
            f"Подключено несколько аккаунтов — укажите нужный: -a ИМЯ. "
            f"Подключены: {_names(accounts)}."
        )
    return accounts[0]


def parse_when(raw: str, now: datetime, tz: tzinfo) -> datetime:
    """Время публикации, введённое человеком, — в UTC.

    Понимает «19:00», «сегодня 19:00», «завтра 19:00», «26.10 19:00»,
    «26.10.2026 19:00» и «2026-10-26 19:00». Время без даты — ближайшее
    будущее: если сегодня оно прошло, то завтра. Дата без года — тоже.
    """
    text = " ".join(raw.strip().lower().split())
    today = now.astimezone(tz).date()
    try:
        if match := re.fullmatch(r"(?:(сегодня|завтра) )?(\d{1,2}):(\d{2})", text):
            word, hour, minute = match.groups()
            moment = _at(today + timedelta(days=1 if word == "завтра" else 0), hour, minute, tz)
            if word is None and moment <= now:
                moment = _at(today + timedelta(days=1), hour, minute, tz)
        elif match := re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))? (\d{1,2}):(\d{2})", text):
            day, month, year, hour, minute = match.groups()
            moment = _at(date(int(year or today.year), int(month), int(day)), hour, minute, tz)
            if year is None and moment <= now:
                moment = _at(date(today.year + 1, int(month), int(day)), hour, minute, tz)
        elif match := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})[ t](\d{1,2}):(\d{2})", text):
            year, month, day, hour, minute = match.groups()
            moment = _at(date(int(year), int(month), int(day)), hour, minute, tz)
        else:
            raise ValueError(text)
    except ValueError:
        raise RuntimeError(
            f"Не понял время {raw!r}. Примеры: 19:00, завтра 19:00, 26.10 19:00, 26.10.2026 19:00."
        ) from None
    if moment <= now:
        raise RuntimeError(f"Время {report.when(moment, tz, now=now)} уже прошло.")
    return moment


def _at(day: date, hour: str, minute: str, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(int(hour), int(minute)), tzinfo=tz).astimezone(timezone.utc)


def _all_accounts(db: Database) -> list[Row]:
    accounts = db.accounts()
    if not accounts:
        raise RuntimeError(
            "Нет подключённых аккаунтов. Сначала войдите: python -m tiktok_manager login"
        )
    return accounts


def _names(accounts: Sequence[Row]) -> str:
    return ", ".join(account_label(account) for account in accounts)


def _item(db: Database, item_id: int) -> Row:
    item = db.item(item_id)
    if item is None:
        raise RuntimeError(f"В очереди нет #{item_id}. Номера показывает команда queue.")
    return item


def _require_publish_scope(account: Row, mode: str) -> None:
    scope = PUBLISH_SCOPES[mode]
    if scope not in set(account["scope"].split(",")):
        raise RuntimeError(
            f"У подключения нет права {scope}, без которого не работает режим {mode}. "
            "Добавьте его в Scopes приложения и войдите заново: python -m tiktok_manager login"
        )


def _expand_files(paths: Sequence[str]) -> list[Path]:
    """Файлы роликов; папка раскрывается во все видео внутри неё."""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found = sorted(
                child
                for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in MIME_TYPES
            )
            if not found:
                raise RuntimeError(f"В папке {path} нет роликов MP4, MOV или WebM.")
            files.extend(found)
        else:
            files.append(path)
    return files


def _caption_for(video: VideoFile, explicit: str | None) -> str:
    """Подпись: из --caption, иначе из файла с тем же именем и расширением .txt."""
    if explicit is not None:
        caption = explicit.strip()
    else:
        sidecar = video.path.with_suffix(".txt")
        try:
            caption = sidecar.read_text(encoding="utf-8-sig").strip() if sidecar.is_file() else ""
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Не удалось прочитать подпись из {sidecar}: {exc}") from None
    length = caption_length(caption)
    if length > CAPTION_LIMIT:
        raise RuntimeError(
            f"{video.path.name}: подпись длиной {length} символов, а TikTok принимает "
            f"до {CAPTION_LIMIT}."
        )
    return caption


def _creator_preview(config: Config, db: Database, open_id: str) -> dict[str, Any] | None:
    """Настройки аккаунта в TikTok, чтобы поймать проблемы до постановки в очередь.

    Если TikTok сейчас не отвечает, это не повод не ставить ролик: перед
    публикацией настройки проверятся ещё раз.
    """
    config.require_app()
    try:
        return TikTokClient(config, db).creator_info(open_id)
    except LoginRequired:
        raise
    except TikTokError as exc:
        print(f"Не удалось проверить аккаунт в TikTok: {exc}\nПроверю ещё раз перед публикацией.")
        return None


def _positive(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("нужно целое число") from None
    if value < 1:
        raise argparse.ArgumentTypeError("нужно число больше нуля")
    return value


def _force_utf8_output() -> None:
    """Переводит вывод в UTF-8.

    В консоли Windows по умолчанию кодировка не UTF-8, и первая же строка с
    русским текстом падает с UnicodeEncodeError. На Linux и macOS вызов
    ничего не меняет.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Поток перенаправлен так, что его не перенастроить — не беда.
            pass
