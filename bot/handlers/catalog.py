"""Каталог проверок: человек изучает всё сам, до взятия задания."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..callbacks import CatalogCB
from ..catalog import PRODUCTS, check_by_code, checks_for_product, product_by_code
from ..utils import render_check

router = Router(name="catalog")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(Command("catalog"))
async def cmd_catalog(message: Message, state: FSMContext) -> None:
    # Каталог можно открыть посреди анкеты — тогда диалог сбрасываем,
    # иначе ответы человека уйдут в брошенную анкету.
    await state.clear()
    await message.answer(
        texts.CATALOG_INTRO, reply_markup=keyboards.catalog_products(PRODUCTS)
    )


@router.callback_query(CatalogCB.filter(F.action == "products"))
async def show_products(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_text(
        texts.CATALOG_INTRO, reply_markup=keyboards.catalog_products(PRODUCTS)
    )


@router.callback_query(CatalogCB.filter(F.action == "product"))
async def show_product(callback: CallbackQuery, callback_data: CatalogCB) -> None:
    await callback.answer()
    if callback.message is None:
        return

    product = product_by_code(callback_data.code)
    if product is None:
        await callback.message.edit_text(
            texts.CATALOG_INTRO, reply_markup=keyboards.catalog_products(PRODUCTS)
        )
        return

    checks = checks_for_product(product.code)
    await callback.message.edit_text(
        texts.PRODUCT_CARD.format(
            title=escape(product.title), summary=escape(product.summary)
        ),
        reply_markup=keyboards.catalog_checks(checks),
    )


@router.callback_query(CatalogCB.filter(F.action == "check"))
async def show_check(callback: CallbackQuery, callback_data: CatalogCB) -> None:
    await callback.answer()
    if callback.message is None:
        return

    check = check_by_code(callback_data.code)
    if check is None:
        await callback.message.edit_text(
            texts.CATALOG_INTRO, reply_markup=keyboards.catalog_products(PRODUCTS)
        )
        return

    # Карточка проверки длиннее лимита подписи, поэтому отправляем новым
    # сообщением, а не правим предыдущее: так человек может пролистать назад.
    await callback.message.answer(
        render_check(check), reply_markup=keyboards.catalog_back(check.product)
    )
