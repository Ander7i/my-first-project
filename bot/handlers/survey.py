"""Анкета исполнителя: город, возраст, авто, ИП, связь, продукты."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..callbacks import SurveyCB
from ..catalog import PRODUCTS
from ..db import Database
from ..states import Survey
from ..utils import profile_card

router = Router(name="survey")
router.message.filter(F.chat.type == ChatType.PRIVATE)

# Банковские продукты оформляют с 18 лет, раньше задание выполнить нельзя.
MIN_AGE = 18
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
    await message.answer(texts.ASK_CAR, reply_markup=keyboards.yes_no("car"))


@router.callback_query(Survey.car, SurveyCB.filter(F.action == "car"))
async def got_car(
    callback: CallbackQuery, callback_data: SurveyCB, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(has_car=callback_data.value == "1")
    await state.set_state(Survey.ip)
    await callback.message.edit_text(
        texts.ASK_IP, reply_markup=keyboards.yes_no("ip")
    )


@router.callback_query(Survey.ip, SurveyCB.filter(F.action == "ip"))
async def got_ip(
    callback: CallbackQuery, callback_data: SurveyCB, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(has_ip=callback_data.value == "1")
    await state.set_state(Survey.contact)
    await callback.message.edit_text(
        texts.ASK_CONTACT, reply_markup=keyboards.skip_contact()
    )


@router.message(Survey.contact, F.text)
async def got_contact(message: Message, state: FSMContext) -> None:
    await state.update_data(contact=(message.text or "").strip()[:100])
    await _ask_products(message, state)


@router.callback_query(Survey.contact, SurveyCB.filter(F.action == "skip_contact"))
async def skip_contact(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(contact="")
    await callback.message.edit_text("Способ связи пропущен.")
    await _ask_products(callback.message, state)


async def _ask_products(message: Message, state: FSMContext) -> None:
    await state.update_data(products=[])
    await state.set_state(Survey.products)
    await message.answer(
        texts.ASK_PRODUCTS,
        reply_markup=keyboards.products(PRODUCTS, []),
    )


@router.callback_query(Survey.products, SurveyCB.filter(F.action == "product"))
async def toggle_product(
    callback: CallbackQuery, callback_data: SurveyCB, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is None:
        return

    code = callback_data.value
    if not any(p.code == code for p in PRODUCTS):
        return

    data = await state.get_data()
    chosen: list[str] = list(data.get("products", []))
    if code in chosen:
        chosen.remove(code)
    else:
        chosen.append(code)
    await state.update_data(products=chosen)

    await callback.message.edit_reply_markup(
        reply_markup=keyboards.products(PRODUCTS, chosen)
    )


@router.callback_query(Survey.products, SurveyCB.filter(F.action == "products_done"))
async def products_done(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    data = await state.get_data()
    chosen: list[str] = list(data.get("products", []))
    if not chosen:
        await callback.answer(texts.PRODUCTS_EMPTY, show_alert=True)
        return

    await callback.answer()
    await db.save_profile(
        callback.from_user.id,
        city=data["city"],
        age=data["age"],
        has_car=bool(data.get("has_car")),
        has_ip=bool(data.get("has_ip")),
        contact=data.get("contact", ""),
        products=chosen,
    )
    await state.clear()

    row = await db.get_user(callback.from_user.id)
    await callback.message.edit_text("Анкета заполнена ✅")
    if row is not None:
        await callback.message.answer(texts.SURVEY_DONE.format(profile=profile_card(row)))


@router.message(Survey.car)
async def car_needs_button(message: Message) -> None:
    await message.answer(texts.ASK_CAR, reply_markup=keyboards.yes_no("car"))


@router.message(Survey.ip)
async def ip_needs_button(message: Message) -> None:
    await message.answer(texts.ASK_IP, reply_markup=keyboards.yes_no("ip"))


@router.message(Survey.products)
async def products_need_buttons(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(
        texts.ASK_PRODUCTS,
        reply_markup=keyboards.products(PRODUCTS, list(data.get("products", []))),
    )
