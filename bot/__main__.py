"""Запуск бота: python -m bot"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from . import catalog
from .config import Config, load_config
from .db import Database
from .handlers import build_router
from .reminders import run_reminders
from .utils import contact_url

log = logging.getLogger("bot")

USER_COMMANDS = [
    BotCommand(command="start", description="Начать и заполнить анкету"),
    BotCommand(command="catalog", description="Каталог проверок — что нужно делать"),
    BotCommand(command="available", description="Открытые задания для меня"),
    BotCommand(command="tasks", description="Мои задания"),
    BotCommand(command="profile", description="Моя анкета"),
    BotCommand(command="survey", description="Заполнить анкету заново"),
    BotCommand(command="help", description="Как это работает"),
    BotCommand(command="stop", description="Удалить мои данные"),
]

ADMIN_COMMANDS = [
    *USER_COMMANDS,
    BotCommand(command="admin", description="Панель администратора"),
    BotCommand(command="newtask", description="Создать задание"),
    BotCommand(command="tasklist", description="Список заданий"),
    BotCommand(command="reports", description="Отчёты на проверке"),
    BotCommand(command="payouts", description="К выплате"),
    BotCommand(command="post", description="Опубликовать набор в группу"),
    BotCommand(command="announce", description="Сообщение всем исполнителям"),
]


async def setup_commands(bot: Bot, config: Config) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as exc:
            # Админ ещё не открывал бота — меню появится после первого /start.
            log.warning("Меню для админа %s не установлено: %s", admin_id, exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # Каталог — источник правды о заданиях. Если в нём опечатка в коде
    # продукта, лучше узнать это при старте, а не когда человек нажмёт кнопку.
    catalog.validate()

    config = load_config()
    session = _build_session(config)

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(
        token=config.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )

    # Всё после connect() — под finally: иначе рабочий поток aiosqlite
    # не даёт процессу завершиться, и упавший при старте бот просто висит.
    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен. Админы: %s", me.username, config.admin_ids)
        if config.group_chat_id is None:
            log.warning(
                "GROUP_CHAT_ID не задан — публикации в группу недоступны. "
                "Добавьте бота в группу админом и напишите там /chatid."
            )
        if contact_url(config.support_contact) is None:
            log.warning(
                "SUPPORT_CONTACT не задан или не похож на ник или ссылку. "
                "Заданиям без своей ссылки бот не сможет подсказать, к кому "
                "обратиться за ней."
            )

        await setup_commands(bot, config)

        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher["config"] = config
        dispatcher["db"] = db
        dispatcher.include_router(build_router())

        # Напоминания живут отдельной задачей рядом с polling.
        reminders_task = asyncio.create_task(run_reminders(bot, db, config))

        try:
            # Сбрасываем накопившиеся апдейты, чтобы бот не отвечал на старые.
            await bot.delete_webhook(drop_pending_updates=True)
            await dispatcher.start_polling(
                bot, allowed_updates=["message", "callback_query"]
            )
        finally:
            reminders_task.cancel()
            # Ждём отмену: иначе задача оборвётся посреди запроса к базе.
            with suppress(asyncio.CancelledError):
                await reminders_task
    finally:
        await db.close()
        await bot.session.close()


def _build_session(config: Config) -> AiohttpSession | None:
    if not config.proxy_url:
        return None
    try:
        session = AiohttpSession(proxy=config.proxy_url)
    except RuntimeError as exc:
        raise RuntimeError(
            "Для работы через прокси нужен пакет aiohttp-socks:\n"
            "    pip install aiohttp-socks\n"
            "Если прокси не нужен — уберите PROXY_URL из .env и очистите "
            f"переменную окружения HTTPS_PROXY.\nИсходная ошибка: {exc}"
        ) from exc
    log.info("Подключаюсь к Telegram через прокси")
    return session


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен")
    except TelegramUnauthorizedError:
        raise SystemExit(
            "Telegram отклонил токен. Проверьте BOT_TOKEN в .env — "
            "его выдаёт @BotFather командой /mybots → выберите бота → API Token."
        ) from None
    except RuntimeError as exc:
        # Понятные ошибки настройки показываем без длинного traceback.
        raise SystemExit(str(exc)) from None
