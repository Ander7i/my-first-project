"""Фабрики callback-данных для инлайн-кнопок."""

from aiogram.filters.callback_data import CallbackData


class ConsentCB(CallbackData, prefix="consent"):
    agree: bool


class SurveyCB(CallbackData, prefix="srv"):
    action: str  # car | ip | skip_contact | product | products_done
    value: str = ""


class CatalogCB(CallbackData, prefix="cat"):
    action: str  # products | product | check
    code: str = ""


class TaskCB(CallbackData, prefix="task"):
    action: str  # view | claim | close | reopen | to_group | send
    task_id: int


class NewTaskCB(CallbackData, prefix="nt"):
    action: str  # product | check | skip_notes | create_send | create_only | cancel
    code: str = ""


class AckCB(CallbackData, prefix="ack"):
    agree: bool
    task_id: int


class AssignCB(CallbackData, prefix="asg"):
    action: str  # report | accept | rework | paid
    assignment_id: int


class ReportCB(CallbackData, prefix="rep"):
    action: str  # photos_done | skip_comment | submit | cancel


class StopCB(CallbackData, prefix="stop"):
    confirm: bool
