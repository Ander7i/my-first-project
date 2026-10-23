"""Тесты форматирования, экранирования и автоответов в группе."""

from __future__ import annotations

from bot.db import normalize_city
from bot.handlers.group import _match_faq
from bot.utils import bullets, numbered


def test_numbered_items():
    assert numbered(["Раз", "Два"]) == "1. Раз\n2. Два"


def test_numbered_empty():
    assert numbered([]) == "—"


def test_bullets_items():
    assert bullets(["Раз", "Два"]) == "• Раз\n• Два"


def test_bullets_empty():
    assert bullets([]) == "• —"


def test_formatting_escapes_html():
    """Текст от пользователя не должен ломать разметку сообщения."""
    assert numbered(["<b>жирный</b>"]) == "1. &lt;b&gt;жирный&lt;/b&gt;"
    assert bullets(["a & b"]) == "• a &amp; b"


# ------------------------------------------------------------ автоответы в группе


def test_faq_matches_payment_question():
    match = _match_faq("Ребят, а сколько платят за одну проверку?")
    assert match is not None
    assert "Оплата указана в каждом задании" in match[1]


def test_faq_matches_regardless_of_yo():
    assert _match_faq("а когда заплатят?") is not None


def test_faq_scam_answer_names_what_we_never_ask():
    match = _match_faq("это развод какой-то")
    assert match is not None
    answer = match[1].lower()
    assert "пин" in answer or "реквизиты" in answer
    assert "коды из смс" in answer


def test_faq_answers_dropper_question_with_refusal():
    """Про передачу карты ответ должен быть однозначным «нет»."""
    match = _match_faq("а можно оформить на себя и передать карту?")
    assert match is not None
    answer = match[1].lower()
    assert "нет" in answer
    assert "обнал" in answer


def test_faq_discloses_credit_history_effect():
    match = _match_faq("а это испортит кредитную историю?")
    assert match is not None
    assert "кредитной истории" in match[1].lower()


def test_faq_explains_ip_consequences():
    match = _match_faq("а нужно ли ип для этого?")
    assert match is not None
    answer = match[1].lower()
    assert "взнос" in answer
    assert "закрыва" in answer or "закры" in answer


def test_faq_handles_russian_inflection():
    """Подстрочный поиск не знает падежей, поэтому ключи — основы слов."""
    for phrasing in (
        "это испортит кредитную историю?",
        "а кредитная история не пострадает",
        "что будет с кредитной историей",
    ):
        match = _match_faq(phrasing)
        assert match is not None, phrasing
        assert "кредитной истории" in match[1].lower()

    for phrasing in ("сколько платят", "сколько заплатите", "какая оплата"):
        match = _match_faq(phrasing)
        assert match is not None, phrasing
        assert "зависит от продукта" in match[1]


def test_faq_routes_payout_and_amount_to_different_topics():
    """«когда оплата» — про сроки выплаты, «какая оплата» — про размер."""
    when = _match_faq("когда оплата придет?")
    how_much = _match_faq("какая оплата за проверку?")
    assert when is not None and how_much is not None
    assert when[0] != how_much[0]
    assert "После проверки отчёта" in when[1]
    assert "зависит от продукта" in how_much[1]


def test_faq_does_not_mistake_tip_for_ip():
    """«тип» содержит «ип» — короткий ключ отвечал бы про взносы ИП."""
    for phrasing in (
        "какой тип проверки бывает",
        "это какой-то новый тип заданий",
        "в принципе понятно",
    ):
        match = _match_faq(phrasing)
        assert match is None or "взнос" not in match[1], phrasing

    # При этом настоящие вопросы про ИП по-прежнему распознаются.
    for phrasing in (
        "нужно ли ип для этого",
        "а регистрация ип обязательна",
        "можно закрыть ип потом?",
    ):
        assert _match_faq(phrasing) is not None, phrasing


def test_faq_mfo_answer_separates_safe_and_loan_tasks():
    """В группе нельзя отвечать про МФО одной строкой: задания разные."""
    match = _match_faq("а что там по мфо?")
    assert match is not None
    answer = match[1].lower()
    # Безопасные задания названы отдельно от задания с долгом.
    assert "без денег" in answer
    assert "реальным займом" in answer or "реальный займ" in answer
    # Названы оба последствия, которые люди недооценивают.
    assert "в срок" in answer
    assert "кредитной истории" in answer
    assert "ипотек" in answer
    # И отказ переводить деньги дальше.
    assert "долг останется" in answer


def test_faq_mfo_matches_common_phrasings():
    for phrasing in (
        "можно взять микрозайм?",
        "что за задания с займами",
        "а займ надо брать?",
        "это про мфо?",
    ):
        match = _match_faq(phrasing)
        assert match is not None, phrasing
        assert "МФО" in match[1], phrasing


def test_faq_returns_none_for_unrelated_text():
    assert _match_faq("всем привет, как погода") is None


def test_faq_topics_are_distinct():
    """Разные вопросы должны попадать в разные темы (для антифлуда)."""
    payment = _match_faq("сколько платят")
    duties = _match_faq("что надо делать")
    assert payment is not None and duties is not None
    assert payment[0] != duties[0]


# ----------------------------------------------------------------- нормализация


def test_normalize_city():
    assert normalize_city("  Москва ") == "москва"
    assert normalize_city("САНКТ-ПЕТЕРБУРГ") == "санкт-петербург"
    assert normalize_city("Нижний   Новгород") == "нижний новгород"
    assert normalize_city("") == ""


def test_normalize_city_treats_yo_as_e():
    """«Орёл» и «Орел» — один город."""
    assert normalize_city("Орёл") == normalize_city("Орел")
