"""Поведение бота в группе: автоответы на вопросы, приветствие, /chatid.

Чтобы бот видел обычные сообщения группы (а не только команды и упоминания),
в @BotFather нужно выключить privacy mode: /setprivacy → выбрать бота → Disable.
Без этого автоответы работают только когда бота упомянули или ответили на его
сообщение — остальная логика не меняется.
"""

from __future__ import annotations

import logging
import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from .. import keyboards, texts
from ..config import Config

router = Router(name="group")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

log = logging.getLogger(__name__)

# Один и тот же вопрос не переобъясняем чаще раза в 10 минут,
# и в целом бот пишет в группу не чаще раза в 20 секунд.
TOPIC_COOLDOWN = 600
CHAT_COOLDOWN = 20

_last_topic: dict[tuple[int, int], float] = {}
_last_message: dict[int, float] = {}


def _allowed(chat_id: int, topic: int) -> bool:
    now = time.monotonic()
    if now - _last_message.get(chat_id, 0.0) < CHAT_COOLDOWN:
        return False
    if now - _last_topic.get((chat_id, topic), 0.0) < TOPIC_COOLDOWN:
        return False
    _last_message[chat_id] = now
    _last_topic[(chat_id, topic)] = now
    return True


def _match_faq(text: str) -> tuple[int, str] | None:
    """Ищет первый подходящий вопрос из FAQ. Возвращает (индекс темы, ответ)."""
    lowered = text.lower().replace("ё", "е")
    for index, (keywords, answer) in enumerate(texts.FAQ):
        for keyword in keywords:
            if keyword.replace("ё", "е") in lowered:
                return index, answer
    return None


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    """Помогает узнать ID группы для GROUP_CHAT_ID в .env."""
    await message.reply(
        f"ID этого чата: <code>{message.chat.id}</code>\n"
        "Впишите его в .env как GROUP_CHAT_ID и перезапустите бота."
    )


@router.message(Command("vacancy"))
async def cmd_vacancy(message: Message, bot: Bot, config: Config) -> None:
    """Публикация объявления о наборе прямо из группы (только для админов бота)."""
    if message.from_user is None or not config.is_admin(message.from_user.id):
        return
    me = await bot.get_me()
    await message.answer(
        texts.GROUP_VACANCY, reply_markup=keyboards.open_bot(me.username or "")
    )


@router.message(F.new_chat_members)
async def welcome(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        await message.answer(
            texts.GROUP_WELCOME.format(name=escape(member.full_name)),
            reply_markup=keyboards.open_bot(me.username or ""),
        )


@router.message(F.text)
async def auto_reply(message: Message, bot: Bot) -> None:
    """Отвечает на типовые вопросы о подработке."""
    text = message.text or ""
    me = await bot.get_me()

    mentioned = bool(
        me.username and f"@{me.username}".lower() in text.lower()
    ) or bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == me.id
    )

    match = _match_faq(text)

    if match is not None:
        topic, answer = match
        if not _allowed(message.chat.id, topic):
            return
        await message.reply(
            answer, reply_markup=keyboards.open_bot(me.username or "")
        )
        return

    # Обратились к боту, но вопрос непонятный — отвечаем общей подсказкой.
    if mentioned and _allowed(message.chat.id, -1):
        await message.reply(
            texts.GROUP_FALLBACK, reply_markup=keyboards.open_bot(me.username or "")
        )
