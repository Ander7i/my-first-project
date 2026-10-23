"""Роутеры бота.

Порядок важен: сначала команды и анкета, потом групповые автоответы.
Внутри приватного чата команды из start регистрируются первыми, поэтому
/stop и /help работают даже посреди незаконченного диалога.
"""

from aiogram import Router

from . import admin, group, performer, start, survey


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(survey.router)
    root.include_router(admin.router)
    root.include_router(performer.router)
    root.include_router(group.router)
    return root


__all__ = ["build_router"]
