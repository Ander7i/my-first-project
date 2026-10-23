"""Фабрики callback-данных для инлайн-кнопок."""

from aiogram.filters.callback_data import CallbackData


class ConsentCB(CallbackData, prefix="consent"):
    agree: bool


class SurveyCB(CallbackData, prefix="srv"):
    action: str  # car | skip_contact | cat | cat_done
    value: str = ""


class TaskCB(CallbackData, prefix="task"):
    action: str  # claim | view | close | reopen | to_group | send
    task_id: int


class AssignCB(CallbackData, prefix="asg"):
    action: str  # report | accept | rework | paid | view
    assignment_id: int


class ReportCB(CallbackData, prefix="rep"):
    action: str  # photos_done | skip_comment | submit | cancel


class StopCB(CallbackData, prefix="stop"):
    confirm: bool
