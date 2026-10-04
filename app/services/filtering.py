"""Relevance filters for found listings (F2 keywords with morphology, F3 regions and budget)."""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache

import pymorphy3

from app.services.settings_store import Filters

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=50_000)
def lemma(word: str) -> str:
    word = word.lower().replace("ё", "е")
    if word.isupper() or len(word) <= 3 or not re.search("[а-я]", word):
        return word  # abbreviations (ПД, РД, КТ) and latin as is
    return _morph().parse(word)[0].normal_form.replace("ё", "е")


def lemmas(text: str) -> list[str]:
    return [lemma(w) for w in WORD_RE.findall(text or "")]


def _phrase_in(phrase: str, words: list[str]) -> bool:
    target = lemmas(phrase)
    if not target:
        return False
    n = len(target)
    return any(words[i : i + n] == target for i in range(len(words) - n + 1))


@dataclass
class FilterResult:
    relevant: bool
    reasons: list[str] = field(default_factory=list)
    matched_work: list[str] = field(default_factory=list)
    matched_object: list[str] = field(default_factory=list)
    stop_hits: list[str] = field(default_factory=list)


def _region_match(region: str | None, items: list[str]) -> bool:
    if not region:
        return False
    lower = region.lower()
    return any(item.strip().lower() in lower for item in items if item.strip())


def check(text: str, filters: Filters, *, region: str | None = None, budget: Decimal | None = None) -> FilterResult:
    # Abbreviations must match case-sensitively («РД» but not the preposition-like «рд» in other words).
    words = lemmas(text)
    raw_words = WORD_RE.findall(text or "")
    work = [
        w for w in filters.work_words if (w.isupper() and w in raw_words) or (not w.isupper() and _phrase_in(w, words))
    ]
    objects = [
        w
        for w in filters.object_words
        if (w.isupper() and w in raw_words) or (not w.isupper() and _phrase_in(w, words))
    ]
    stops = [w for w in filters.stop_words if _phrase_in(w, words)]
    result = FilterResult(relevant=True, matched_work=work, matched_object=objects, stop_hits=stops)
    if not work:
        result.relevant = False
        result.reasons.append("нет признаков проектных работ")
    if not objects:
        result.relevant = False
        result.reasons.append("нет признаков медицинского объекта")
    if stops and len(stops) >= len(work):
        result.relevant = False
        result.reasons.append("стоп-слова: " + ", ".join(stops))
    if filters.regions_deny and _region_match(region, filters.regions_deny):
        result.relevant = False
        result.reasons.append(f"регион в чёрном списке: {region}")
    if filters.regions_allow and region and not _region_match(region, filters.regions_allow):
        result.relevant = False
        result.reasons.append(f"регион не в белом списке: {region}")
    if budget is None:
        if not filters.keep_unknown_budget:
            result.relevant = False
            result.reasons.append("бюджет не указан")
    else:
        if filters.budget_min is not None and budget < filters.budget_min:
            result.relevant = False
            result.reasons.append(f"бюджет ниже {filters.budget_min:,} ₽".replace(",", " "))
        if filters.budget_max is not None and budget > filters.budget_max:
            result.relevant = False
            result.reasons.append(f"бюджет выше {filters.budget_max:,} ₽".replace(",", " "))
    return result


BUDGET_RE = re.compile(
    r"(?:начальн\w*\s+(?:\(максимальн\w*\)\s+)?цен\w*|НМЦ\w*|бюджет|стоимост\w*|цен[аы])[^0-9]{0,40}"
    r"(\d[\d\s ]{0,15}(?:[.,]\d{1,2})?)\s*(млн|тыс)?",
    re.IGNORECASE,
)


def extract_budget(text: str) -> Decimal | None:
    match = BUDGET_RE.search(text or "")
    if not match:
        return None
    raw = re.sub(r"[\s ]", "", match.group(1)).replace(",", ".")
    try:
        value = Decimal(raw)
    except ArithmeticError:
        return None
    unit = (match.group(2) or "").lower()
    if unit == "млн":
        value *= 1_000_000
    elif unit == "тыс":
        value *= 1_000
    return value.quantize(Decimal("1")) if value >= 1000 else None
