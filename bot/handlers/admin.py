"""Панель администратора: задания, проверка отчётов, выплаты, рассылки."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards, texts
from ..callbacks import AssignCB, SurveyCB, TaskCB
from ..config import Config
from ..db import ACCEPTED, PAID, REWORK, SUBMITTED, Database
from ..states import Announce, NewTask, Rework
from ..utils import (
    broadcast,
    format_checklist,
    performer_name,
    send_safe,
    task_card,
)

router = Router(name="admin")
router.message.filter(F.chat.type == ChatType.PRIVATE)

log = logging.getLogger(__name__)


def _is_admin(user_id: int | None, config: Config) -> bool:
    return user_id is not None and config.is_admin(user_id)


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, config: Config) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return
    stats = await db.stats()
    await message.answer(texts.ADMIN_MENU.format(**stats))


# ------------------------------------------------------------ создание задания


@router.message(Command("newtask"))
async def cmd_newtask(message: Message, state: FSMContext, config: Config) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return
    await state.clear()
    await state.set_state(NewTask.title)
    await message.answer(texts.NEW_TASK_TITLE)


@router.message(NewTask.title, F.text)
async def task_title(message: Message, state: FSMContext, config: Config) -> None:
    await state.update_data(title=(message.text or "").strip()[:120])
    await state.set_state(NewTask.category)
    await message.answer(
        texts.NEW_TASK_CATEGORY,
        reply_markup=keyboards.new_task_categories(config.categories),
    )


@router.callback_query(NewTask.category, SurveyCB.filter(F.action == "newtask_cat"))
async def task_category(
    callback: CallbackQuery,
    callback_data: SurveyCB,
    state: FSMContext,
    config: Config,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    try:
        category = config.categories[int(callback_data.value)]
    except (ValueError, IndexError):
        return
    await state.update_data(category=category)
    await state.set_state(NewTask.city)
    await callback.message.edit_text(f"Категория: {escape(category)}")
    await callback.message.answer(texts.NEW_TASK_CITY)


@router.message(NewTask.city, F.text)
async def task_city(message: Message, state: FSMContext) -> None:
    await state.update_data(city=(message.text or "").strip()[:60])
    await state.set_state(NewTask.payment)
    await message.answer(texts.NEW_TASK_PAYMENT)


@router.message(NewTask.payment, F.text)
async def task_payment(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(texts.NEW_TASK_NUMBER_INVALID)
        return
    await state.update_data(payment=int(raw))
    await state.set_state(NewTask.deadline)
    await message.answer(texts.NEW_TASK_DEADLINE)


@router.message(NewTask.deadline, F.text)
async def task_deadline(message: Message, state: FSMContext) -> None:
    await state.update_data(deadline=(message.text or "").strip()[:60])
    await state.set_state(NewTask.slots)
    await message.answer(texts.NEW_TASK_SLOTS)


@router.message(NewTask.slots, F.text)
async def task_slots(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(texts.NEW_TASK_NUMBER_INVALID)
        return
    await state.update_data(slots=int(raw))
    await state.set_state(NewTask.description)
    await message.answer(texts.NEW_TASK_DESCRIPTION)


@router.message(NewTask.description, F.text)
async def task_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=(message.text or "").strip()[:3000])
    await state.set_state(NewTask.checklist)
    await message.answer(texts.NEW_TASK_CHECKLIST)


@router.message(NewTask.checklist, F.text)
async def task_checklist(message: Message, state: FSMContext) -> None:
    await state.update_data(checklist=(message.text or "").strip()[:4000])
    data = await state.get_data()
    await state.set_state(NewTask.preview)

    preview = texts.NEW_TASK_PREVIEW.format(
        card=task_card(data),
        description=escape(data.get("description", "")),
        checklist=format_checklist(data.get("checklist", "")),
    )
    await message.answer(preview, reply_markup=keyboards.confirm_new_task())


@router.callback_query(NewTask.preview, TaskCB.filter(F.action == "create_cancel"))
async def task_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text(texts.TASK_CREATE_CANCELLED)


@router.callback_query(
    NewTask.preview, TaskCB.filter(F.action.in_({"create_send", "create_only"}))
)
async def task_create(
    callback: CallbackQuery,
    callback_data: TaskCB,
    state: FSMContext,
    db: Database,
    bot: Bot,
) -> None:
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return

    data = await state.get_data()
    task_id = await db.create_task(data, callback.from_user.id)
    await state.clear()

    await callback.message.edit_text(f"Задание #{task_id} сохранено.")

    if callback_data.action == "create_only":
        await callback.message.answer(
            f"Чтобы разослать его позже — /tasklist и выберите задание #{task_id}."
        )
        return

    sent, failed = await dispatch_task(bot, db, task_id)
    if sent == 0 and failed == 0:
        await callback.message.answer(
            texts.TASK_NO_MATCHES.format(
                task_id=task_id, city=escape(data["city"]), category=escape(data["category"])
            )
        )
    else:
        await callback.message.answer(
            texts.TASK_PUBLISHED.format(task_id=task_id, sent=sent, failed=failed)
        )


async def dispatch_task(bot: Bot, db: Database, task_id: int) -> tuple[int, int]:
    """Рассылает задание подходящим исполнителям. Возвращает (доставлено, нет)."""
    task = await db.get_task(task_id)
    if task is None:
        return 0, 0

    users = await db.match_users(task["city"], task["category"])
    if not users:
        return 0, 0

    taken = await db.taken_slots(task_id)
    free = max(task["slots"] - taken, 0)
    card = task_card(task, free_slots=free) + "\n" + escape(task["description"] or "")

    sent = failed = 0
    for user in users:
        ok = await send_safe(
            bot,
            user["tg_id"],
            "🆕 <b>Новое задание для вас</b>\n\n" + card,
            reply_markup=keyboards.claim_task(task_id),
            db=db,
        )
        if ok:
            sent += 1
        else:
            failed += 1
    return sent, failed


# -------------------------------------------------------------- список заданий


@router.message(Command("tasklist"))
async def cmd_tasklist(message: Message, db: Database, config: Config) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return

    tasks = await db.list_tasks(limit=15)
    if not tasks:
        await message.answer("Заданий пока нет. Создать: /newtask")
        return

    for task in tasks:
        taken = await db.taken_slots(task["id"])
        free = max(task["slots"] - taken, 0)
        status = "🟢 открыто" if task["status"] == "open" else "🔒 закрыто"
        await message.answer(
            f"#{task['id']} {status}\n" + task_card(task, free_slots=free),
            reply_markup=keyboards.task_actions(task["id"], task["status"]),
        )


@router.callback_query(TaskCB.filter(F.action == "send"))
async def task_resend(
    callback: CallbackQuery, callback_data: TaskCB, db: Database, bot: Bot
) -> None:
    await callback.answer("Рассылаю…")
    if callback.message is None:
        return
    sent, failed = await dispatch_task(bot, db, callback_data.task_id)
    await callback.message.answer(
        texts.TASK_PUBLISHED.format(task_id=callback_data.task_id, sent=sent, failed=failed)
    )


@router.callback_query(TaskCB.filter(F.action.in_({"close", "reopen"})))
async def task_toggle(
    callback: CallbackQuery, callback_data: TaskCB, db: Database
) -> None:
    new_status = "closed" if callback_data.action == "close" else "open"
    await db.set_task_status(callback_data.task_id, new_status)
    await callback.answer("Готово")
    if callback.message is None:
        return
    task = await db.get_task(callback_data.task_id)
    if task is None:
        return
    taken = await db.taken_slots(task["id"])
    label = "🟢 открыто" if new_status == "open" else "🔒 закрыто"
    await callback.message.edit_text(
        f"#{task['id']} {label}\n" + task_card(task, free_slots=max(task["slots"] - taken, 0)),
        reply_markup=keyboards.task_actions(task["id"], new_status),
    )


# ----------------------------------------------------------- проверка отчётов


@router.message(Command("reports"))
async def cmd_reports(message: Message, db: Database, config: Config) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return

    rows = await db.list_assignments_by_status(SUBMITTED)
    if not rows:
        await message.answer(texts.NO_REPORTS)
        return

    for row in rows:
        await message.answer(
            f"🕓 Отчёт #{row['id']} по «{escape(row['title'])}»\n"
            f"Исполнитель: {performer_name(row)}\n"
            f"💰 {row['payment']} ₽",
            reply_markup=keyboards.review_report(row["id"]),
        )


@router.callback_query(AssignCB.filter(F.action == "accept"))
async def accept_report(
    callback: CallbackQuery,
    callback_data: AssignCB,
    db: Database,
    bot: Bot,
    config: Config,
) -> None:
    if not _is_admin(callback.from_user.id if callback.from_user else None, config):
        await callback.answer(texts.NOT_ADMIN, show_alert=True)
        return

    assignment = await db.get_assignment(callback_data.assignment_id)
    if assignment is None:
        await callback.answer("Отчёт не найден.", show_alert=True)
        return

    await db.set_assignment_status(callback_data.assignment_id, ACCEPTED)
    await callback.answer("Принято")

    await send_safe(
        bot,
        assignment["user_id"],
        texts.REPORT_ACCEPTED_USER.format(
            title=escape(assignment["title"]), payment=assignment["payment"]
        ),
        db=db,
    )
    if callback.message is not None:
        await callback.message.answer(
            texts.ADMIN_ACCEPTED,
            reply_markup=keyboards.mark_paid(callback_data.assignment_id),
        )


@router.callback_query(AssignCB.filter(F.action == "rework"))
async def rework_ask(
    callback: CallbackQuery,
    callback_data: AssignCB,
    state: FSMContext,
    config: Config,
) -> None:
    if not _is_admin(callback.from_user.id if callback.from_user else None, config):
        await callback.answer(texts.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    if callback.message is None:
        return
    await state.set_state(Rework.note)
    await state.update_data(assignment_id=callback_data.assignment_id)
    await callback.message.answer(texts.ADMIN_REWORK_ASK)


@router.message(Rework.note, F.text)
async def rework_send(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    data = await state.get_data()
    assignment_id = int(data["assignment_id"])
    note = (message.text or "").strip()[:1000]
    await state.clear()

    assignment = await db.get_assignment(assignment_id)
    if assignment is None:
        await message.answer("Отчёт не найден.")
        return

    await db.set_assignment_status(assignment_id, REWORK, note=note)
    await send_safe(
        bot,
        assignment["user_id"],
        texts.REPORT_REWORK_USER.format(
            title=escape(assignment["title"]), note=escape(note)
        ),
        db=db,
    )
    await message.answer(texts.ADMIN_REWORK_SENT)


# -------------------------------------------------------------------- выплаты


@router.message(Command("payouts"))
async def cmd_payouts(message: Message, db: Database, config: Config) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return

    rows = await db.list_assignments_by_status(ACCEPTED)
    if not rows:
        await message.answer(texts.NO_PAYOUTS)
        return

    total = sum(row["payment"] for row in rows)
    await message.answer(f"К выплате: <b>{total} ₽</b> ({len(rows)} шт.)")
    for row in rows:
        await message.answer(
            f"💰 {row['payment']} ₽ — {performer_name(row)}\n"
            f"Задание: {escape(row['title'])}",
            reply_markup=keyboards.mark_paid(row["id"]),
        )


@router.callback_query(AssignCB.filter(F.action == "paid"))
async def mark_paid(
    callback: CallbackQuery,
    callback_data: AssignCB,
    db: Database,
    bot: Bot,
    config: Config,
) -> None:
    if not _is_admin(callback.from_user.id if callback.from_user else None, config):
        await callback.answer(texts.NOT_ADMIN, show_alert=True)
        return

    assignment = await db.get_assignment(callback_data.assignment_id)
    if assignment is None:
        await callback.answer("Не найдено.", show_alert=True)
        return

    await db.set_assignment_status(callback_data.assignment_id, PAID)
    await callback.answer("Отмечено")

    await send_safe(
        bot,
        assignment["user_id"],
        texts.REPORT_PAID_USER.format(
            payment=assignment["payment"], title=escape(assignment["title"])
        ),
        db=db,
    )
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as exc:
            log.debug("Не смог убрать кнопку: %s", exc)
        await callback.message.answer(texts.ADMIN_PAID)


# ------------------------------------------------------- публикации и рассылки


@router.message(Command("post"))
async def cmd_post(message: Message, bot: Bot, config: Config) -> None:
    """Публикует в группе объявление о наборе с кнопкой на анкету."""
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return
    if config.group_chat_id is None:
        await message.answer(texts.POST_NO_GROUP)
        return

    me = await bot.get_me()
    try:
        await bot.send_message(
            config.group_chat_id,
            texts.GROUP_VACANCY,
            reply_markup=keyboards.open_bot(me.username or ""),
        )
    except Exception as exc:
        await message.answer(texts.POST_FAILED.format(error=escape(str(exc))))
        return
    await message.answer(texts.POST_DONE)


@router.callback_query(TaskCB.filter(F.action == "to_group"))
async def task_to_group(
    callback: CallbackQuery,
    callback_data: TaskCB,
    db: Database,
    bot: Bot,
    config: Config,
) -> None:
    """Анонс задания в группе — без кнопки «взять», только приглашение в анкету."""
    if not _is_admin(callback.from_user.id if callback.from_user else None, config):
        await callback.answer(texts.NOT_ADMIN, show_alert=True)
        return
    if config.group_chat_id is None:
        await callback.answer(texts.POST_NO_GROUP, show_alert=True)
        return

    task = await db.get_task(callback_data.task_id)
    if task is None:
        await callback.answer(texts.TASK_GONE, show_alert=True)
        return

    me = await bot.get_me()
    try:
        await bot.send_message(
            config.group_chat_id,
            texts.GROUP_TASK_ANNOUNCE.format(
                city=escape(task["city"]),
                category=escape(task["category"]),
                payment=task["payment"],
                deadline=escape(task["deadline"]),
                title=escape(task["title"]),
            ),
            reply_markup=keyboards.open_bot(me.username or ""),
        )
    except Exception as exc:
        await callback.answer("Ошибка публикации", show_alert=True)
        if callback.message is not None:
            await callback.message.answer(texts.POST_FAILED.format(error=escape(str(exc))))
        return
    await callback.answer("Опубликовано в группе")


@router.message(Command("announce"))
async def cmd_announce(message: Message, state: FSMContext, config: Config) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None, config):
        await message.answer(texts.NOT_ADMIN)
        return
    await state.set_state(Announce.text)
    await message.answer(texts.ANNOUNCE_ASK)


@router.message(Announce.text, F.text)
async def announce_send(
    message: Message, state: FSMContext, db: Database, bot: Bot
) -> None:
    await state.clear()
    text = message.html_text or message.text or ""

    async with db.conn.execute(
        "SELECT tg_id FROM users WHERE status = 'active' AND consent_at IS NOT NULL"
    ) as cur:
        chat_ids = [row["tg_id"] for row in await cur.fetchall()]

    sent, failed = await broadcast(bot, db, chat_ids, text)
    await message.answer(texts.ANNOUNCE_DONE.format(sent=sent, failed=failed))
