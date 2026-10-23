"""Роутеры бота.

Порядок важен. Команды из start идут первыми, поэтому /stop и /help
работают даже посреди незаконченного диалога. Каталог — следом, чтобы
/catalog открывался из любого состояния. Групповые автоответы последними:
они ловят любой текст в группе.
"""

from aiogram import Router

from . import admin, catalog, group, performer, start, survey


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(catalog.router)
    root.include_router(survey.router)
    root.include_router(admin.router)
    root.include_router(performer.router)
    root.include_router(group.router)
    return root


__all__ = ["build_router"]
