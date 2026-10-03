"""Detect government / municipal customers: such listings are excluded (constraint 3.1 of the spec)."""

import re

# Organisational-legal forms and words that mark state or municipal bodies.
GOV_PATTERNS = [
    r"\b(?:[А-Я]{0,3}ГБУЗ|ГБУ|ФГБУ|ФГБОУ|ФГАОУ|ФГУП|ГУП|МУП|ГАУЗ|ГАУ|МАУ|МБУ|МБУЗ|МКУ|ФКУ|ГКУ|ФБУН|ФБУЗ|ОГБУЗ|КГБУЗ|ОГАУЗ|ГОБУЗ|ФГБНУ)\b",
    r"\bгосударственн\w*\s+(?:бюджетн|казенн|казённ|автономн|унитарн)\w*",
    r"\bмуниципальн\w*\s+(?:бюджетн|казенн|казённ|автономн|унитарн)\w*",
    r"\bминистерств\w*",
    r"\bдепартамент\w*\s+здравоохранени\w*",
    r"\bкомитет\w*\s+по\s+здравоохранени\w*",
    r"\bадминистраци\w*\s+(?:города|района|муниципальн|городского|сельского)",
    r"\bфедеральн\w*\s+(?:государственн|казенн|бюджетн)\w*",
]
GOV_RE = re.compile("|".join(GOV_PATTERNS), re.IGNORECASE)
# Procurement laws for state customers.
GOV_LAW_RE = re.compile(r"\b(?:44|223)\s*-?\s*ФЗ\b|zakupki\.gov\.ru", re.IGNORECASE)


def government_reason(*texts: str | None) -> str | None:
    """Return a human-readable reason if any text points to a government customer, else None."""
    for text in texts:
        if not text:
            continue
        if match := GOV_RE.search(text):
            return f"госзаказчик: «{match.group(0).strip()}»"
        if match := GOV_LAW_RE.search(text):
            return f"госзакупка: «{match.group(0).strip()}»"
    return None
