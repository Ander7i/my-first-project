"""Знакомство, согласие, профиль, удаление данных."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..callbacks import ConsentCB, StopCB
from ..db import CANCELLED, CLAIMED, REWORK, SUBMITTED, Database
from ..states import Survey
from ..utils import profile_card

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return

    await db.touch_user(user.id, user.username, user.full_name)
    row = await db.get_user(user.id)

    # Анкета уже заполнена — показываем её, а не гоняем по вопросам заново.
    if row is not None and row["status"] == "active":
        await message.answer(texts.ALREADY_REGISTERED.format(profile=profile_card(row)))
        return

    # Согласие уже было (например, бросил анкету на середине) — продолжаем с вопросов.
    if row is not None and row["consent_at"]:
        await state.set_state(Survey.city)
        await message.answer(texts.ASK_CITY)
        return

    await message.answer(texts.CONSENT, reply_markup=keyboards.consent())


@router.callback_query(ConsentCB.filter())
async def on_consent(
    callback: CallbackQuery,
    callback_data: ConsentCB,
    state: FSMContext,
    db: Database,
) -> None:
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return

    if not callback_data.agree:
        await state.clear()
        await callback.message.edit_text(texts.CONSENT_DECLINED)
        return

    await db.set_consent(callback.from_user.id)
    await callback.message.edit_text("Спасибо! Пара вопросов 👇")
    await state.set_state(Survey.city)
    await callback.message.answer(texts.ASK_CITY)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext, db: Database) -> None:
    if message.from_user is None:
        return
    row = await db.get_user(message.from_user.id)
    if row is None or row["status"] != "active":
        await message.answer(texts.NEED_SURVEY)
        return
    await message.answer(
        profile_card(row) + "\nЧтобы изменить анкету, отправьте /survey."
    )


@router.message(Command("survey"))
async def cmd_survey(message: Message, state: FSMContext, db: Database) -> None:
    """Повторное заполнение анкеты."""
    if message.from_user is None:
        return
    row = await db.get_user(message.from_user.id)
    if row is None or not row["consent_at"]:
        await message.answer(texts.NEED_SURVEY)
        return
    await state.set_state(Survey.city)
    await message.answer(texts.ASK_CITY)


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    """Нужна, чтобы владелец узнал свой ID для ADMIN_IDS."""
    if message.from_user is None:
        return
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext, db: Database) -> None:
    if message.from_user is None:
        return
    row = await db.get_user(message.from_user.id)
    if row is None:
        await message.answer("У меня нет ваших данных.")
        return
    await state.clear()
    await message.answer(texts.STOP_CONFIRM, reply_markup=keyboards.confirm_stop())


@router.callback_query(StopCB.filter())
async def on_stop(
    callback: CallbackQuery,
    callback_data: StopCB,
    db: Database,
) -> None:
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return

    if not callback_data.confirm:
        await callback.message.edit_text(texts.STOP_CANCELLED)
        return

    # Снимаем незакрытые задания, чтобы слоты вернулись в пул.
    active = await db.list_user_assignments(
        callback.from_user.id, (CLAIMED, SUBMITTED, REWORK)
    )
    for row in active:
        await db.set_assignment_status(row["id"], CANCELLED)

    await db.delete_user(callback.from_user.id)
    await callback.message.edit_text(texts.STOP_DONE)
