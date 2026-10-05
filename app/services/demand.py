"""Tell a customer's request for design work from a designer's own advertising, news or articles.

Web search returns many pages of design firms («проектирование медицинских центров — наши услуги»). They contain
the same keywords as real requests, so search results need a demand check before they become listings.
"""

import re
from dataclasses import dataclass, field

DEMAND_PATTERNS = {
    "требуется": r"\bтребу(?:ется|ются)\b",
    "ищем исполнителя": (
        r"\bищ(?:ем|у|ет)\s+(?:\w+\s+){0,2}"
        r"(?:проектировщик|подрядчик|исполнител|генпроектировщик|проектн\w+\s+организаци)"
    ),
    "нужен проект": r"\bнуж(?:ен|на|но|ны)\s+(?:\w+\s+){0,2}(?:проект|спроектировать|разработ)",
    "тендер": r"\bтендер\w*",
    "закупка": r"\bзакупк\w*",
    "запрос предложений": r"\bзапрос\w*\s+(?:коммерческ\w+\s+|ценов\w+\s+)?(?:предложени|котировок|цен)\w*",
    "конкурс": r"\bконкурс\w*\b",
    "приглашение подрядчиков": r"\bприглаша\w+\s+(?:к\s+участию|подрядчик|проектн|организаци|компани)",
    "техническое задание": r"\bтехническ\w+\s+задани\w*|\bТЗ\b",
    "срок подачи": r"\b(?:срок|окончани\w*)\s+(?:подачи|приёма|приема)\b|\bпри[её]м\s+(?:заявок|предложений)",
    "лот": r"\bлот\w*\s*№?\s*\d",
    "извещение": r"\bизвещени\w*",
}
SUPPLY_PATTERNS = {
    "наши услуги": r"\bнаши\s+(?:услуг|проект|работ|объект|специалист|преимуществ)\w*",
    "мы проектируем": r"\bмы\s+(?:проектируем|выполняем|разрабатываем|предлагаем|оказываем|делаем|спроектируем)",
    "заказать проект": r"\bзаказать\s+(?:\w+\s+)?(?:проект|проектировани|услуг|расч[её]т)\w*",
    "цены от": r"\b(?:стоимость|цен[аы])\s+(?:\w+\s+){0,3}от\s+\d",
    "портфолио": r"\bпортфолио\b",
    "консультация": r"\b(?:бесплатн\w+\s+)?консультаци\w*",
    "оставьте заявку": r"\bоставь(?:те)?\s+заявку\b",
    "опыт компании": (
        r"\b(?:лет\s+на\s+рынке|опыт\s+(?:работы\s+)?более|выполнено\s+более"
        r"|более\s+\d+\s+(?:проектов|объектов))"
    ),
    "прайс": r"\bпрайс\w*",
}
DEMAND_RE = {k: re.compile(v, re.IGNORECASE) for k, v in DEMAND_PATTERNS.items()}
SUPPLY_RE = {k: re.compile(v, re.IGNORECASE) for k, v in SUPPLY_PATTERNS.items()}


@dataclass
class DemandCheck:
    is_request: bool
    demand: list[str] = field(default_factory=list)
    supply: list[str] = field(default_factory=list)
    reason: str | None = None


def check_demand(text: str) -> DemandCheck:
    demand = [k for k, rx in DEMAND_RE.items() if rx.search(text or "")]
    supply = [k for k, rx in SUPPLY_RE.items() if rx.search(text or "")]
    if not demand:
        return DemandCheck(False, demand, supply, "нет признаков заявки заказчика (похоже на статью или рекламу)")
    if len(supply) > len(demand):
        return DemandCheck(False, demand, supply, "похоже на рекламу услуг проектировщика: " + ", ".join(supply[:3]))
    return DemandCheck(True, demand, supply)
