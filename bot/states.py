"""Состояния диалогов (FSM)."""

from aiogram.fsm.state import State, StatesGroup


class Survey(StatesGroup):
    """Анкета исполнителя."""

    city = State()
    age = State()
    car = State()
    contact = State()
    categories = State()


class NewTask(StatesGroup):
    """Создание задания администратором."""

    title = State()
    category = State()
    city = State()
    payment = State()
    deadline = State()
    slots = State()
    description = State()
    checklist = State()
    preview = State()


class Report(StatesGroup):
    """Сдача отчёта исполнителем."""

    photos = State()
    answers = State()
    comment = State()
    preview = State()


class Rework(StatesGroup):
    """Администратор пишет причину доработки."""

    note = State()


class Announce(StatesGroup):
    """Рассылка по исполнителям."""

    text = State()
