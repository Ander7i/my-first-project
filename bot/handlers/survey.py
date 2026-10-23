"""Анкета исполнителя: город, возраст, авто, связь, категории."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..callbacks import SurveyCB
from ..config import Config
from ..db import Database
from ..states import Survey
from ..utils import profile_card

router = Router(name="survey")
router.message.filter(F.chat.type == ChatType.PRIVATE)

MIN_AGE = 16
MAX_AGE = 90


@router.message(Survey.city, F.text)
async def got_city(message: Message, state: FSMContext) -> None:
    city = (message.text or "").strip()
    if len(city) < 2:
        await message.answer(texts.ASK_CITY_INVALID)
        return
    await state.update_data(city=city)
    await state.set_state(Survey.age)
    await message.answer(texts.ASK_AGE)


@router.message(Survey.age, F.text)
async def got_age(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (MIN_AGE <= int(raw) <= MAX_AGE):
        await message.answer(texts.ASK_AGE_INVALID)
        return
    await state.update_data(age=int(raw))
    await state.set_state(Survey.car)
    await message.answer(texts.ASK_CAR, reply_markup=keyboards.car())


@router.callback_query(Survey.car, SurveyCB.filter(F.action == "car"))
async def got_car(
    callback: CallbackQuery, callback_data: SurveyCB, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(has_car=callback_data.value == "1")
    await state.set_state(Survey.contact)
    await callback.message.edit_text(
        texts.ASK_CONTACT, reply_markup=keyboards.skip_contact()
    )


@router.message(Survey.contact, F.text)
async def got_contact(message: Message, state: FSMContext, config: Config) -> None:
    await state.update_data(contact=(message.text or "").strip()[:100])
    await _ask_categories(message, state, config)


@router.callback_query(Survey.contact, SurveyCB.filter(F.action == "skip_contact"))
async def skip_contact(
    callback: CallbackQuery, state: FSMContext, config: Config
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(contact="")
    await callback.message.edit_text(texts.ASK_CONTACT.split("\n")[0] + " — пропущено.")
    await _ask_categories(callback.message, state, config)


async def _ask_categories(message: Message, state: FSMContext, config: Config) -> None:
    await state.update_data(categories=[])
    await state.set_state(Survey.categories)
    await message.answer(
        texts.ASK_CATEGORIES,
        reply_markup=keyboards.categories(config.categories, []),
    )


@router.callback_query(Survey.categories, SurveyCB.filter(F.action == "cat"))
async def toggle_category(
    callback: CallbackQuery,
    callback_data: SurveyCB,
    state: FSMContext,
    config: Config,
) -> None:
    await callback.answer()
    if callback.message is None:
        return

    try:
        name = config.categories[int(callback_data.value)]
    except (ValueError, IndexError):
        return

    data = await state.get_data()
    chosen: list[str] = list(data.get("categories", []))
    if name in chosen:
        chosen.remove(name)
    else:
        chosen.append(name)
    await state.update_data(categories=chosen)

    await callback.message.edit_reply_markup(
        reply_markup=keyboards.categories(config.categories, chosen)
    )


@router.callback_query(Survey.categories, SurveyCB.filter(F.action == "cat_done"))
async def categories_done(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    data = await state.get_data()
    chosen: list[str] = list(data.get("categories", []))
    if not chosen:
        await callback.answer(texts.CATEGORIES_EMPTY, show_alert=True)
        return

    await callback.answer()
    await db.save_profile(
        callback.from_user.id,
        city=data["city"],
        age=data["age"],
        has_car=bool(data.get("has_car")),
        contact=data.get("contact", ""),
        categories=chosen,
    )
    await state.clear()

    row = await db.get_user(callback.from_user.id)
    await callback.message.edit_text("Анкета заполнена ✅")
    if row is not None:
        await callback.message.answer(
            texts.SURVEY_DONE.format(profile=profile_card(row))
        )


@router.message(Survey.car)
async def car_needs_button(message: Message) -> None:
    await message.answer(texts.ASK_CAR, reply_markup=keyboards.car())


@router.message(Survey.categories)
async def categories_need_buttons(message: Message, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    await message.answer(
        texts.ASK_CATEGORIES,
        reply_markup=keyboards.categories(config.categories, list(data.get("categories", []))),
    )
