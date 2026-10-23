"""Тесты разбора сроков. Логика с датами ошибкоопасная, поэтому подробно."""

from __future__ import annotations

from datetime import date

from bot.dates import (
    days_until,
    format_due_date,
    from_iso,
    is_sane_due_date,
    parse_due_date,
    to_iso,
)

BASE = date(2026, 10, 23)


def test_full_numeric_date():
    assert parse_due_date("30.10.2026", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("30/10/2026", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("05.11.2026", base=BASE) == date(2026, 11, 5)


def test_two_digit_year():
    assert parse_due_date("30.10.27", base=BASE) == date(2027, 10, 30)


def test_date_without_year_takes_nearest_future():
    # Ещё не прошло в этом году.
    assert parse_due_date("30.10", base=BASE) == date(2026, 10, 30)
    # Уже прошло — значит следующий год.
    assert parse_due_date("01.03", base=BASE) == date(2027, 3, 1)


def test_month_by_name():
    assert parse_due_date("30 октября", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("5 ноября", base=BASE) == date(2026, 11, 5)
    assert parse_due_date("1 марта", base=BASE) == date(2027, 3, 1)
    assert parse_due_date("15 января 2027", base=BASE) == date(2027, 1, 15)


def test_preposition_is_ignored():
    assert parse_due_date("до 30 октября", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("до 30.10.2026", base=BASE) == date(2026, 10, 30)


def test_relative_days():
    assert parse_due_date("через 7 дней", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("7 дней", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("7", base=BASE) == date(2026, 10, 30)


def test_relative_words():
    assert parse_due_date("завтра", base=BASE) == date(2026, 10, 24)
    assert parse_due_date("послезавтра", base=BASE) == date(2026, 10, 25)
    assert parse_due_date("сегодня", base=BASE) == BASE


def test_yo_and_case_do_not_matter():
    assert parse_due_date("ЧЕРЕЗ 7 ДНЕЙ", base=BASE) == date(2026, 10, 30)
    assert parse_due_date("  30 Октября  ", base=BASE) == date(2026, 10, 30)


def test_invalid_input_returns_none():
    for value in (
        "",
        "   ",
        "скоро",
        "до конца месяца",
        "как получится",
        "32.10.2026",
        "30.13.2026",
        "0",
        "пятница",
    ):
        assert parse_due_date(value, base=BASE) is None, value


def test_impossible_date_is_rejected():
    """31 февраля не должно превращаться в 3 марта."""
    assert parse_due_date("31.02.2027", base=BASE) is None
    assert parse_due_date("30 февраля", base=BASE) is None


def test_leap_day_is_accepted():
    assert parse_due_date("29.02.2028", base=BASE) == date(2028, 2, 29)


def test_too_far_future_rejected_as_relative():
    assert parse_due_date("через 500 дней", base=BASE) is None


def test_sanity_check():
    assert is_sane_due_date(date(2026, 10, 30), base=BASE)
    assert is_sane_due_date(BASE, base=BASE)
    # Прошлое не годится.
    assert not is_sane_due_date(date(2026, 10, 22), base=BASE)
    # Слишком далеко.
    assert not is_sane_due_date(date(2030, 1, 1), base=BASE)


def test_format_includes_year_only_when_needed():
    assert format_due_date(date(2026, 10, 30)).startswith("до 30 октября")
    # Текущий год в тестах — настоящий, поэтому проверяем через явный год.
    assert "2029" in format_due_date(date(2029, 10, 30))


def test_iso_round_trip():
    value = date(2026, 10, 30)
    assert from_iso(to_iso(value)) == value


def test_from_iso_rejects_garbage():
    assert from_iso("") is None
    assert from_iso("не дата") is None
    assert from_iso("2026-13-40") is None


def test_days_until():
    assert days_until(date(2026, 10, 30), base=BASE) == 7
    assert days_until(BASE, base=BASE) == 0
    assert days_until(date(2026, 10, 20), base=BASE) == -3
