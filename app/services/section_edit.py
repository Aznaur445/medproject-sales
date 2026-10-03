"""Owner edits section prices by text (Telegram): «АР 250000», «OVIK = 1,2 млн», «ПБ 0» (remove)."""

import re
from decimal import Decimal

from app.core.numbers import parse_amount
from app.services.proposal_doc import money

LINE_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁё]+)\s*[:=—–-]?\s*(.+?)\s*$")
ABBR_RE = re.compile(r"\(([A-Za-zА-Яа-яЁё/]+)\)")


def aliases(sections: dict[str, str]) -> dict[str, str]:
    """code -> name  =>  lowercase alias -> code. Aliases: the code and the abbreviation in brackets (АР, ОВиК)."""
    result: dict[str, str] = {}
    for code, name in sections.items():
        result[code.lower()] = code
        for abbr in ABBR_RE.findall(name):
            for part in abbr.split("/"):
                result[part.lower()] = code
    return result


def parse_section_prices(text: str, sections: dict[str, str]) -> dict[str, Decimal]:
    """Return {code: price}; price 0 means «remove the section». Raises ValueError with all problems."""
    known = aliases(sections)
    result: dict[str, Decimal] = {}
    errors: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("•-*").strip()
        if not line or line.lower().startswith("итого"):
            continue
        match = LINE_RE.match(line)
        if not match:
            errors.append(f"не понял строку «{line}»")
            continue
        alias, value = match.group(1).lower(), match.group(2)
        code = known.get(alias)
        if code is None:
            errors.append(f"неизвестный раздел «{match.group(1)}»")
            continue
        value = value.split(":")[-1]  # tolerate «AR — Архитектурные решения: 250000»
        if re.fullmatch(r"\s*(0|-|—|убрать|нет)\s*", value, re.IGNORECASE):
            result[code] = Decimal("0")
            continue
        try:
            result[code] = parse_amount(value)
        except ValueError:
            errors.append(f"«{line}»: не понял сумму")
    if errors:
        raise ValueError("; ".join(errors))
    if not result:
        raise ValueError("не нашёл ни одной строки вида «АР 250000»")
    return result


def format_sections(current: list[dict], available: dict[str, str]) -> str:
    lines = []
    total = Decimal("0")
    for item in current:
        price = Decimal(str(item["price"]))
        total += price
        lines.append(f"{item['code']} — {item['name']}: {money(price)}")
    lines.append(f"Итого: {money(total)}")
    extra = [f"{code} — {name}" for code, name in available.items() if code not in {i["code"] for i in current}]
    if extra:
        lines.append("\nМожно добавить:\n" + "\n".join(extra))
    return "\n".join(lines)
