"""Сторона исполнителя: взять задание, сдать отчёт."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from .. import keyboards, texts
from ..callbacks import AssignCB, ReportCB, TaskCB
from ..config import Config
from ..db import (
    ACCEPTED,
    CLAIMED,
    PAID,
    REWORK,
    SUBMITTED,
    Database,
)
from ..states import Report
from ..utils import (
    format_answers,
    format_checklist,
    parse_checklist,
    performer_name,
)

router = Router(name="performer")
router.message.filter(F.chat.type == ChatType.PRIVATE)

log = logging.getLogger(__name__)

# Telegram принимает не больше 10 медиа в одном альбоме.
MEDIA_GROUP_LIMIT = 10
MAX_PHOTOS = 20


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, db: Database) -> None:
    if message.from_user is None:
        return
    user = await db.get_user(message.from_user.id)
    if user is None or user["status"] != "active":
        await message.answer(texts.NEED_SURVEY)
        return

    rows = await db.list_user_assignments(
        message.from_user.id, (CLAIMED, SUBMITTED, ACCEPTED, REWORK, PAID)
    )
    if not rows:
        await message.answer(texts.NO_TASKS)
        return

    lines = [texts.MY_TASKS_HEADER, ""]
    for row in rows:
        label = texts.STATUS_LABELS.get(row["status"], row["status"])
        lines.append(
            f"#{row['task_id']} <b>{escape(row['title'])}</b> — {label}\n"
            f"   💰 {row['payment']} ₽ · ⏳ {escape(row['deadline'])}"
        )
        if row["status"] == REWORK and row["note"]:
            lines.append(f"   ♻️ {escape(row['note'])}")

    await message.answer("\n".join(lines), reply_markup=keyboards.my_tasks(rows))


@router.callback_query(TaskCB.filter(F.action == "claim"))
async def claim(
    callback: CallbackQuery,
    callback_data: TaskCB,
    db: Database,
) -> None:
    if callback.from_user is None:
        await callback.answer()
        return

    user = await db.get_user(callback.from_user.id)
    if user is None or user["status"] != "active":
        await callback.answer(texts.NEED_SURVEY, show_alert=True)
        return

    _, code = await db.claim_task(callback_data.task_id, callback.from_user.id)

    if code == "no_task":
        await callback.answer(texts.TASK_GONE, show_alert=True)
        return
    if code == "closed":
        await callback.answer(texts.TASK_CLOSED, show_alert=True)
        return
    if code == "no_slots":
        await callback.answer(texts.TASK_TAKEN, show_alert=True)
        return
    if code == "already":
        await callback.answer(texts.TASK_ALREADY, show_alert=True)
        return

    await callback.answer("Задание ваше!")
    task = await db.get_task(callback_data.task_id)
    if task is None or callback.message is None:
        return

    # Убираем кнопку «Взять», чтобы её не нажимали повторно.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        log.debug("Не смог убрать кнопку: %s", exc)

    await callback.message.answer(
        texts.TASK_CLAIMED.format(
            title=escape(task["title"]),
            deadline=escape(task["deadline"]),
            payment=task["payment"],
            description=escape(task["description"] or "—"),
            checklist=format_checklist(task["checklist"]),
        )
    )


# ----------------------------------------------------------------- сдача отчёта


@router.callback_query(AssignCB.filter(F.action == "report"))
async def report_start(
    callback: CallbackQuery,
    callback_data: AssignCB,
    state: FSMContext,
    db: Database,
) -> None:
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return

    assignment = await db.get_assignment(callback_data.assignment_id)
    if assignment is None or assignment["user_id"] != callback.from_user.id:
        await callback.message.answer("Задание не найдено.")
        return
    if assignment["status"] not in (CLAIMED, REWORK):
        await callback.message.answer(
            "По этому заданию отчёт уже сдан — смотрите /tasks."
        )
        return

    questions = parse_checklist(assignment["checklist"])
    await state.set_state(Report.photos)
    await state.set_data(
        {
            "assignment_id": assignment["id"],
            "title": assignment["title"],
            "questions": questions,
            "photos": [],
            "answers": [],
        }
    )
    await callback.message.answer(
        texts.REPORT_START.format(title=escape(assignment["title"])),
        reply_markup=keyboards.photos_done(),
    )


@router.message(Report.photos, F.photo)
async def report_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos: list[str] = list(data.get("photos", []))
    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"Достаточно, принято максимум {MAX_PHOTOS} фото.")
        return

    # message.photo — список размеров одного изображения, берём самый большой.
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(
        texts.REPORT_PHOTO_OK.format(count=len(photos)),
        reply_markup=keyboards.photos_done(),
    )


@router.message(Report.photos, F.document | F.video | F.text)
async def report_photo_wrong(message: Message) -> None:
    await message.answer(texts.REPORT_NOT_PHOTO, reply_markup=keyboards.photos_done())


@router.callback_query(Report.photos, ReportCB.filter(F.action == "photos_done"))
async def photos_done(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return

    data = await state.get_data()
    if not data.get("photos"):
        await callback.answer(texts.REPORT_NEED_PHOTO, show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        log.debug("Не смог убрать кнопку: %s", exc)

    await _ask_next_question(callback.message, state)


async def _ask_next_question(message: Message, state: FSMContext) -> None:
    """Задаёт следующий вопрос чек-листа или переходит к комментарию."""
    data = await state.get_data()
    questions: list[str] = data.get("questions", [])
    answers: list[str] = data.get("answers", [])

    if not questions:
        await state.set_state(Report.comment)
        await message.answer(texts.REPORT_NO_CHECKLIST, reply_markup=keyboards.skip_comment())
        return

    if len(answers) >= len(questions):
        await state.set_state(Report.comment)
        await message.answer(texts.REPORT_COMMENT, reply_markup=keyboards.skip_comment())
        return

    step = len(answers) + 1
    await state.set_state(Report.answers)
    await message.answer(
        texts.REPORT_QUESTION.format(
            step=step, total=len(questions), question=escape(questions[step - 1])
        )
    )


@router.message(Report.answers, F.text)
async def report_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    answers: list[str] = list(data.get("answers", []))
    answers.append((message.text or "").strip()[:1000])
    await state.update_data(answers=answers)
    await _ask_next_question(message, state)


@router.message(Report.comment, F.text)
async def report_comment(message: Message, state: FSMContext) -> None:
    await state.update_data(comment=(message.text or "").strip()[:2000])
    await _show_preview(message, state)


@router.callback_query(Report.comment, ReportCB.filter(F.action == "skip_comment"))
async def report_skip_comment(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await state.update_data(comment="")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:
        log.debug("Не смог убрать кнопку: %s", exc)
    await _show_preview(callback.message, state)


async def _show_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    questions: list[str] = data.get("questions", [])
    answers: list[str] = data.get("answers", [])

    blocks = []
    for question, answer in zip(questions, answers, strict=False):
        blocks.append(f"<b>{escape(question)}</b>\n{escape(answer)}")
    if not questions and answers:
        blocks.append(escape(answers[0]))
    if data.get("comment"):
        blocks.append(f"\n💬 <i>{escape(data['comment'])}</i>")

    await state.set_state(Report.preview)
    await message.answer(
        texts.REPORT_PREVIEW.format(
            title=escape(data.get("title", "")),
            photos=len(data.get("photos", [])),
            answers="\n\n".join(blocks) if blocks else "— без ответов",
        ),
        reply_markup=keyboards.report_preview(),
    )


@router.callback_query(Report.preview, ReportCB.filter(F.action == "submit"))
async def report_submit(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    config: Config,
) -> None:
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return

    data = await state.get_data()
    assignment_id = int(data["assignment_id"])

    # На доработке мог остаться прошлый отчёт — заменяем его целиком.
    await db.clear_report(assignment_id)

    position = 0
    for file_id in data.get("photos", []):
        await db.add_report_item(assignment_id, "photo", file_id=file_id, position=position)
        position += 1

    questions: list[str] = data.get("questions", [])
    answers: list[str] = data.get("answers", [])
    for question, answer in zip(questions, answers, strict=False):
        await db.add_report_item(assignment_id, "answer", text=question, position=position)
        position += 1
        await db.add_report_item(assignment_id, "reply", text=answer, position=position)
        position += 1
    if not questions and answers:
        await db.add_report_item(assignment_id, "reply", text=answers[0], position=position)
        position += 1

    if data.get("comment"):
        await db.add_report_item(
            assignment_id, "comment", text=data["comment"], position=position
        )

    await db.set_assignment_status(assignment_id, SUBMITTED, note="")
    await state.clear()

    await callback.message.edit_text(texts.REPORT_SENT)
    await _send_report_to_admins(bot, db, config, assignment_id)


@router.callback_query(ReportCB.filter(F.action == "cancel"))
async def report_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text(texts.REPORT_CANCELLED)


async def _send_report_to_admins(
    bot: Bot, db: Database, config: Config, assignment_id: int
) -> None:
    """Пересылает отчёт администраторам: сначала фото, затем карточка с кнопками."""
    assignment = await db.get_assignment(assignment_id)
    if assignment is None:
        return

    items = await db.list_report_items(assignment_id)
    photos = [item["file_id"] for item in items if item["kind"] == "photo" and item["file_id"]]

    card = texts.ADMIN_NEW_REPORT.format(
        title=escape(assignment["title"]),
        performer=performer_name(assignment),
        city=escape(assignment["city"]),
        payment=assignment["payment"],
        answers=format_answers(items),
    )

    for admin_id in config.admin_ids:
        try:
            for start in range(0, len(photos), MEDIA_GROUP_LIMIT):
                chunk = photos[start : start + MEDIA_GROUP_LIMIT]
                await bot.send_media_group(
                    admin_id, [InputMediaPhoto(media=file_id) for file_id in chunk]
                )
            await bot.send_message(
                admin_id, card, reply_markup=keyboards.review_report(assignment_id)
            )
        except Exception as exc:
            log.warning("Не смог отправить отчёт админу %s: %s", admin_id, exc)
