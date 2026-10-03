import re
from decimal import Decimal, InvalidOperation


def parse_amount(text: str, *, max_value: Decimal = Decimal("1000000000")) -> Decimal:
    """Parse '1 450 000', '1,45 млн', '950к', '1450000 ₽' into whole roubles."""
    cleaned = re.sub(r"[\s ₽]|руб\.?", "", text.lower()).replace(",", ".")
    multiplier = Decimal("1")
    if cleaned.endswith(("млн", "m")):
        cleaned, multiplier = cleaned.rstrip("млнm"), Decimal("1000000")
    elif cleaned.endswith(("тыс", "к", "k")):
        cleaned, multiplier = cleaned.rstrip("тыскk"), Decimal("1000")
    try:
        value = (Decimal(cleaned) * multiplier).quantize(Decimal("1"))
    except InvalidOperation as exc:
        raise ValueError("Не понял сумму. Пример: 1450000 или 1,45 млн") from exc
    if value <= 0 or value > max_value:
        raise ValueError("Сумма вне разумных пределов")
    return value


def parse_decimal(text: str | None) -> Decimal | None:
    if text is None or not str(text).strip():
        return None
    try:
        return Decimal(str(text).replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"Не число: {text}") from exc
