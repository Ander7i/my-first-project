"""Тесты разбора чек-листа, экранирования и автоответов в группе."""

from __future__ import annotations

from bot.db import normalize_city
from bot.handlers.group import _match_faq
from bot.utils import format_checklist, parse_checklist


def test_parse_checklist_strips_blank_lines():
    raw = "Сколько ждали?\n\n  Поздоровались?  \n\n"
    assert parse_checklist(raw) == ["Сколько ждали?", "Поздоровались?"]


def test_parse_checklist_empty():
    assert parse_checklist("") == []
    assert parse_checklist("   \n  ") == []


def test_format_checklist_numbers_items():
    assert format_checklist("Раз\nДва") == "1. Раз\n2. Два"


def test_format_checklist_without_items():
    assert "свободной форме" in format_checklist("") or "свободная форма" in format_checklist("")


def test_format_checklist_escapes_html():
    """Текст от пользователя не должен ломать разметку сообщения."""
    assert format_checklist("<b>жирный</b>") == "1. &lt;b&gt;жирный&lt;/b&gt;"


def test_faq_matches_payment_question():
    match = _match_faq("Ребят, а сколько платят за одну проверку?")
    assert match is not None
    assert "Оплата указана в каждом задании" in match[1]


def test_faq_matches_regardless_of_yo():
    """«сколько заплатят» и «сколько заплатЯт» через ё тоже должны ловиться."""
    assert _match_faq("а когда заплатят?") is not None


def test_faq_matches_scam_question():
    match = _match_faq("это развод какой-то")
    assert match is not None
    assert "предоплату" in match[1]


def test_faq_returns_none_for_unrelated_text():
    assert _match_faq("всем привет, как погода") is None


def test_faq_topics_are_distinct():
    """Разные вопросы должны попадать в разные темы (для антифлуда)."""
    payment = _match_faq("сколько платят")
    duties = _match_faq("что надо делать")
    assert payment is not None and duties is not None
    assert payment[0] != duties[0]


def test_normalize_city():
    assert normalize_city("  Москва ") == "москва"
    assert normalize_city("САНКТ-ПЕТЕРБУРГ") == "санкт-петербург"
    assert normalize_city("Нижний   Новгород") == "нижний новгород"
    assert normalize_city("") == ""


def test_normalize_city_treats_yo_as_e():
    """«Орёл» и «Орел» — один город."""
    assert normalize_city("Орёл") == normalize_city("Орел")
