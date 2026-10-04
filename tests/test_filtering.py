from decimal import Decimal as D

import pytest

from app.services.filtering import check, extract_budget, lemmas
from app.services.settings_store import Filters

F = Filters()


@pytest.mark.parametrize(
    "text",
    [
        "Разработка проектной документации на капитальный ремонт поликлиники",
        "Проектирование стоматологической клиники 300 м2, разделы АР, ЭОМ, ОВиК",
        "Требуется РД для реконструкции диагностического центра (МРТ, КТ)",
        "Ищем подрядчика: перепланировка помещений под медицинский центр",
    ],
)
def test_relevant(text):
    result = check(text, F)
    assert result.relevant, result.reasons


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Поставка медицинского оборудования для поликлиники, проект договора прилагается", "стоп-слова"),
        ("Проектирование торгового центра", "медицинского объекта"),
        ("Уборка помещений стоматологической клиники", "проектных работ"),
    ],
)
def test_not_relevant(text, reason):
    result = check(text, F)
    assert not result.relevant and any(reason in r for r in result.reasons)


def test_morphology_matches_inflected_forms():
    assert "поликлиника" in lemmas("поликлиниках")
    assert check("Проекты больниц и госпиталей", F).relevant


def test_regions_and_budget():
    f = Filters(regions_allow=["Москва", "Краснодарский"], budget_min=500_000, keep_unknown_budget=False)
    text = "Проектирование клиники"
    assert check(text, f, region="г. Москва", budget=D("900000")).relevant
    assert not check(text, f, region="Новосибирская область", budget=D("900000")).relevant
    assert not check(text, f, region="Москва", budget=D("100000")).relevant
    assert not check(text, f, region="Москва", budget=None).relevant
    deny = Filters(regions_deny=["Чукотка"])
    assert not check(text, deny, region="Чукотский АО, Чукотка").relevant


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Начальная (максимальная) цена договора: 1 250 000,00 руб.", D("1250000")),
        ("Бюджет 2,5 млн", D("2500000")),
        ("Стоимость работ — 950 тыс. руб", D("950000")),
        ("Без цены", None),
    ],
)
def test_extract_budget(text, value):
    assert extract_budget(text) == value
