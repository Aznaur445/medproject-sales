"""Rule-based requirements and contract risk checklist (F9). Works without AI; AI results only add to it."""

import re
from dataclasses import asdict, dataclass

from app.services.settings_store import Capabilities


@dataclass
class Finding:
    kind: str  # requirement / risk
    title: str
    level: str  # low / medium / high; for requirements: ok / missing / check
    quote: str
    recommendation: str


def _quote(text: str, match: re.Match) -> str:
    start, end = max(0, match.start() - 60), min(len(text), match.end() + 80)
    return " ".join(text[start:end].split())


RISK_RULES: list[tuple[str, str, str, str]] = [
    # (title, regex, level, recommendation)
    (
        "Неустойка / штраф",
        r"(неустойк|пени|штраф)\w*[^.]{0,80}?(\d+[.,]?\d*)\s*%",
        "medium",
        "Проверьте размер: нормально не более 0,1% в день и с ограничением 10% от цены. Предложите ограничение.",
    ),
    (
        "Без аванса",
        r"(без\s+аванс|аванс\w*\s+не\s+предусм|оплата\s+по\s+факту|постоплат)",
        "high",
        "Предложите аванс 30–50% или поэтапную оплату: без аванса расходы на субподрядчиков ложатся на вас.",
    ),
    (
        "Отсрочка оплаты",
        r"(?:(?:оплат|расч[её]т)\w*[^.]{0,60}?(?:в\s+течение|не\s+позднее)\s+(?P<days>\d{2,3})\s+"
        r"(?:рабочих|календарных|банковских)?\s*дн|(?:в\s+течение|не\s+позднее)\s+(?P<days2>\d{2,3})\s+"
        r"(?:рабочих|календарных|банковских)?\s*дн\w*[^.]{0,40}(?:оплат|расч[её]т))",
        "medium",
        "Длинная отсрочка платежа: заложите стоимость денег или договоритесь о 10–15 днях.",
    ),
    (
        "Гарантийные обязательства",
        r"гаранти\w*\s+(срок|обязательств)[^.]{0,60}?(\d+)\s*(лет|год|мес)",
        "low",
        "Проверьте срок и объём гарантии на проектную документацию.",
    ),
    (
        "Передача исключительных прав",
        r"исключительн\w*\s+прав",
        "medium",
        "Уточните, что передаются права на использование для данного объекта, без права тиражирования.",
    ),
    (
        "Запрет субподряда",
        r"(без\s+привлечени\w*\s+(третьих|субподряд)|запрещ\w*[^.]{0,30}субподряд|"
        r"лично\s+(исполнител|подрядчик))",
        "high",
        "Вы работаете с ГИП и субподрядчиками: согласуйте право привлекать субподрядчиков.",
    ),
    (
        "Обеспечение заявки / договора",
        r"(обеспечени\w*\s+(заявк|исполнени|договор)|банковск\w*\s+гаранти)",
        "medium",
        "Потребуются средства или банковская гарантия: уточните сумму и стоимость гарантии.",
    ),
    (
        "Прохождение экспертизы за счёт исполнителя",
        r"экспертиз\w*[^.]{0,60}(за\s+сч[её]т\s+(подрядчик|исполнител))",
        "medium",
        "Стоимость экспертизы заложите в цену или исключите из объёма.",
    ),
    (
        "Жёсткие сроки",
        r"(в\s+течение|не\s+более)\s+(\d{1,2})\s+(рабочих|календарных)?\s*дн\w*[^.]{0,40}(выполн|разработ)",
        "medium",
        "Проверьте реальность срока с ГИП; при сжатом сроке применяйте коэффициент срочности.",
    ),
    (
        "Односторонний отказ заказчика",
        r"односторонн\w*[^.]{0,30}отказ",
        "low",
        "Проверьте, что при отказе заказчик оплачивает фактически выполненные работы.",
    ),
]

REQUIREMENT_RULES: list[tuple[str, str, str]] = [
    # (title, regex, capability attribute)
    ("Членство в СРО проектировщиков", r"\bСРО\b|саморегулируем", "has_sro_design"),
    ("Сертификат ISO 9001", r"\bISO\b|ИСО\s*9001|ГОСТ\s+Р\s+ИСО", "has_iso"),
    ("Электронная подпись (ЭЦП/КЭП)", r"\bЭЦП\b|\bКЭП\b|электронн\w*\s+подпис", "has_ecp"),
    (
        "ГИП в национальном реестре специалистов (НОПРИЗ)",
        r"НОПРИЗ|национальн\w*\s+реестр\w*\s+специалист",
        "gip_in_nopriz",
    ),
    ("Опыт аналогичных работ", r"опыт\w*[^.]{0,40}(аналогичн|медицинск|не\s+менее)", "medical_projects_done"),
    ("Выезд на объект / обмеры", r"(выезд|обмер|обследовани)\w*", "can_travel"),
]


def analyze(text: str, caps: Capabilities) -> list[dict]:
    findings: list[Finding] = []
    for title, pattern, level, recommendation in RISK_RULES:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            level_now = level
            if title == "Неустойка / штраф":
                try:
                    percent = float(match.group(2).replace(",", "."))
                    level_now = "high" if percent >= 0.5 else ("medium" if percent >= 0.1 else "low")
                except (ValueError, IndexError):
                    pass
            if title == "Отсрочка оплаты":
                days = int(match.group("days") or match.group("days2"))
                level_now = "high" if days > 30 else "medium"
            findings.append(Finding("risk", title, level_now, _quote(text, match), recommendation))
    advance = re.search(r"аванс\w*[^.]{0,30}?(\d{1,2})\s*%", text, re.IGNORECASE)
    if advance and int(advance.group(1)) < caps.min_advance_percent:
        findings.append(
            Finding(
                "risk",
                f"Аванс {advance.group(1)}%",
                "medium",
                _quote(text, advance),
                f"Аванс ниже вашего минимума {caps.min_advance_percent}%.",
            )
        )
    for title, pattern, attribute in REQUIREMENT_RULES:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = getattr(caps, attribute)
        if isinstance(value, bool):
            status = "ok" if value else "missing"
        else:
            status = "ok" if value > 0 else "check"
        recommendation = {
            "ok": "Есть",
            "missing": "Нет: уточните, обязательно ли, или найдите партнёра с допуском.",
            "check": "Подготовьте подтверждение (договоры, отзывы, кейсы).",
        }[status]
        findings.append(Finding("requirement", title, status, _quote(text, match), recommendation))
    return [asdict(f) for f in findings]
