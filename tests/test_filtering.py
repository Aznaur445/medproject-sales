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
        "Ищем подрядчика: проект перепланировки помещений под медицинский центр",
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
        ("Ищем подрядчика: перепланировка помещений под медицинский центр", "проектных работ"),
    ],
)
def test_not_relevant(text, reason):
    result = check(text, F)
    assert not result.relevant and any(reason in r for r in result.reasons)


def test_morphology_matches_inflected_forms():
    assert "поликлиника" in lemmas("поликлиниках")
    assert check("Проектирование больниц и госпиталей", F).relevant


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


@pytest.mark.parametrize(
    ("text", "relevant", "reason"),
    [
        ("Тендер: разработка проектной и рабочей документации медицинского центра 450 м2", True, None),
        ("Разработка ПСД на кабинет МРТ", True, None),
        ("Проектирование и строительство стоматологической клиники под ключ", True, None),
        ("Строительно-монтажные работы в поликлинике в соответствии с проектной документацией", False, "СМР"),
        ("Поставка медицинского оборудования для клиники. Проект договора прилагается", False, "поставка"),
        ("Капитальный ремонт больницы, проект договора", False, "ремонт"),
        ("Проектирование ветеринарной клиники", False, "ветеринария"),
        ("Проектирование офисного здания", False, "не медицинский"),
    ],
)
def test_strict_medical_design(text, relevant, reason):
    from app.services.relevance import check_medical_design

    result = check_medical_design(text)
    assert result.relevant is relevant
    if reason:
        assert reason in result.reason


def test_design_build_is_tagged():
    from app.services.relevance import check_medical_design

    assert check_medical_design("Проектирование и строительство клиники").tags == ["проектирование + СМР"]
