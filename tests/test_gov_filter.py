import pytest

from app.services.gov_filter import government_reason


@pytest.mark.parametrize(
    "name",
    [
        "ГБУЗ «Городская поликлиника № 5»",
        "ФГБУ НМИЦ",
        "Министерство здравоохранения Краснодарского края",
        "Государственное бюджетное учреждение здравоохранения",
        "ОГБУЗ «Областная больница»",
        "МУП «Горздрав»",
        "Закупка по 44-ФЗ",
        "Закупка по 223 ФЗ",
        "https://zakupki.gov.ru/epz/order/123",
    ],
)
def test_government_detected(name):
    assert government_reason(name) is not None


@pytest.mark.parametrize(
    "name",
    [
        "ООО «Медскан»",
        "ООО «Инвитро-Урал»",
        "АО «Медицина»",
        "ИП Петров, стоматология",
        "Сеть клиник «Здоровье» — проект реконструкции",
    ],
)
def test_commercial_passes(name):
    assert government_reason(name) is None
