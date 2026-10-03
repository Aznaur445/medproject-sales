from decimal import Decimal as D

import pytest

from app.services.section_edit import format_sections, parse_section_prices

SECTIONS = {
    "AR": "Архитектурные решения (АР)",
    "OVIK": "Отопление, вентиляция и кондиционирование (ОВиК)",
    "PB": "Мероприятия по обеспечению пожарной безопасности (ПБ)",
    "POS": "Организация строительства (ПОС/ПОР)",
}


def test_parse_codes_abbreviations_and_amount_formats():
    text = "АР 250 000\novik = 1,2 млн\nПБ: 0\nПОР — 90к\nИтого: 999"
    assert parse_section_prices(text, SECTIONS) == {
        "AR": D("250000"),
        "OVIK": D("1200000"),
        "PB": D("0"),
        "POS": D("90000"),
    }


def test_parse_copied_line_from_bot_list():
    assert parse_section_prices("AR — Архитектурные решения (АР): 300 000 ₽", SECTIONS) == {"AR": D("300000")}


def test_parse_reports_all_errors():
    with pytest.raises(ValueError) as exc:
        parse_section_prices("XX 100\nАР много", SECTIONS)
    assert "XX" in str(exc.value) and "не понял сумму" in str(exc.value)


def test_format_lists_current_and_addable():
    text = format_sections([{"code": "AR", "name": "АР", "price": "250000"}], SECTIONS)
    assert "AR — АР: 250 000 ₽" in text and "Итого" in text and "PB —" in text
