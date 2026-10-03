"""Price calculator (F8): price list -> recommended / minimum price, scenarios and margin.

All money is Decimal. Internal costs and margin never leave this module except to the owner's
screens; proposal documents receive only the final customer-facing section prices.
"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Stage = Literal["pre", "pd", "rd", "extra"]
STAGE_TITLES: dict[str, str] = {
    "pre": "Предпроектная стадия (обследование, обмеры)",
    "pd": "Проектная документация (стадия П)",
    "rd": "Рабочая документация (стадия Р)",
    "extra": "Дополнительные работы",
}
ROUND_TO = Decimal("10000")
ZERO = Decimal("0")


class SectionRate(BaseModel):
    code: str
    name: str
    description: str = ""
    stage: Stage = "rd"
    # Share of the standard package price (curve below). If empty, rate_per_m2 × area is used instead.
    weight: Decimal | None = None
    rate_per_m2: Decimal = ZERO
    min_price: Decimal = ZERO
    cost_share: Decimal = Decimal("0.5")  # what subcontractors cost, share of the section price
    default_selected: bool = True


class Modifier(BaseModel):
    code: str
    name: str
    multiplier: Decimal


class CurvePoint(BaseModel):
    area_m2: Decimal
    price: Decimal


class PriceTableData(BaseModel):
    is_example: bool = False
    sections: list[SectionRate]
    object_types: dict[str, Decimal] = Field(default_factory=dict)  # name -> multiplier
    modifiers: list[Modifier] = Field(default_factory=list)
    regions: dict[str, Decimal] = Field(default_factory=dict)  # region -> multiplier, default 1
    # Price of the standard package (sections with weights summing to 1) by area: the bigger the object,
    # the lower the price per m². Linear between points; power law below the first, last slope above.
    package_curve: list[CurvePoint] = Field(default_factory=list)
    trip_cost: Decimal = Decimal("25000")
    gip_share: Decimal = Decimal("0.10")  # chief project engineer, share of price
    other_costs_share: Decimal = Decimal("0.03")
    tax_rate: Decimal = Decimal("0.07")  # УСН 6% + 1% above 300k ₽ income per year
    target_margin: Decimal = Decimal("0.30")
    min_margin: Decimal = Decimal("0.15")
    max_uplift: Decimal = Decimal("0.15")

    @field_validator("sections")
    @classmethod
    def _unique_codes(cls, sections: list[SectionRate]) -> list[SectionRate]:
        codes = [s.code for s in sections]
        if len(codes) != len(set(codes)):
            raise ValueError("Коды разделов должны быть уникальными")
        return sections

    def overhead_share(self) -> Decimal:
        return self.gip_share + self.other_costs_share + self.tax_rate


class EstimateInput(BaseModel):
    area_m2: Decimal
    object_type: str | None = None
    sections: list[str] | None = None  # None -> sections with default_selected
    modifiers: list[str] = Field(default_factory=list)
    region: str | None = None
    trips: int = 1

    @field_validator("area_m2")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Площадь должна быть больше нуля")
        return v


class SectionPrice(BaseModel):
    code: str
    name: str
    description: str
    stage: Stage
    price: Decimal
    cost: Decimal


class PriceEvaluation(BaseModel):
    price: Decimal
    direct_cost: Decimal
    gip_cost: Decimal
    other_costs: Decimal
    tax: Decimal
    profit: Decimal
    margin: Decimal  # profit / price
    below_minimum: bool


class EstimateResult(BaseModel):
    sections: list[SectionPrice]
    multiplier: Decimal
    package_price: Decimal | None = None
    list_total: Decimal
    direct_cost: Decimal
    min_price: Decimal
    recommended_price: Decimal
    scenarios: dict[str, PriceEvaluation]
    warnings: list[str] = Field(default_factory=list)


def round_money(value: Decimal, step: Decimal = ROUND_TO, rounding: str = ROUND_HALF_UP) -> Decimal:
    return (value / step).quantize(Decimal("1"), rounding=rounding) * step


SMALL_AREA_EXPONENT = 0.35  # below the first curve point price falls slower than area


def package_price(curve: list[CurvePoint], area: Decimal) -> Decimal:
    points = sorted(curve, key=lambda p: p.area_m2)
    if not points:
        raise ValueError("Не задана кривая цены пакета по площади")
    first, last = points[0], points[-1]
    if area <= first.area_m2:
        ratio = float(area / first.area_m2)
        return (first.price * Decimal(str(ratio**SMALL_AREA_EXPONENT))).quantize(Decimal("1"))
    for left, right in zip(points, points[1:], strict=False):
        if area <= right.area_m2:
            share = (area - left.area_m2) / (right.area_m2 - left.area_m2)
            return (left.price + (right.price - left.price) * share).quantize(Decimal("1"))
    if len(points) >= 2:
        prev = points[-2]
        slope = (last.price - prev.price) / (last.area_m2 - prev.area_m2)
    else:
        slope = last.price / last.area_m2
    return (last.price + slope * (area - last.area_m2)).quantize(Decimal("1"))


def evaluate_price(table: PriceTableData, direct_cost: Decimal, price: Decimal, min_price: Decimal) -> PriceEvaluation:
    gip = (price * table.gip_share).quantize(Decimal("1"))
    other = (price * table.other_costs_share).quantize(Decimal("1"))
    tax = (price * table.tax_rate).quantize(Decimal("1"))
    profit = price - direct_cost - gip - other - tax
    margin = (profit / price).quantize(Decimal("0.001")) if price > 0 else ZERO
    return PriceEvaluation(
        price=price,
        direct_cost=direct_cost,
        gip_cost=gip,
        other_costs=other,
        tax=tax,
        profit=profit,
        margin=margin,
        below_minimum=price < min_price,
    )


def calculate(table: PriceTableData, data: EstimateInput) -> EstimateResult:
    warnings: list[str] = []
    multiplier = Decimal("1")
    package = package_price(table.package_curve, data.area_m2) if table.package_curve else None
    if data.object_type:
        if data.object_type in table.object_types:
            multiplier *= table.object_types[data.object_type]
        else:
            warnings.append(f"Тип объекта «{data.object_type}» не найден в расценках, коэффициент 1,0")
    modifiers = {m.code: m for m in table.modifiers}
    for code in data.modifiers:
        if code not in modifiers:
            raise ValueError(f"Неизвестный коэффициент: {code}")
        multiplier *= modifiers[code].multiplier
    if data.region:
        multiplier *= table.regions.get(data.region, Decimal("1"))

    by_code = {s.code: s for s in table.sections}
    codes = data.sections if data.sections is not None else [s.code for s in table.sections if s.default_selected]
    unknown = [c for c in codes if c not in by_code]
    if unknown:
        raise ValueError(f"Неизвестные разделы: {', '.join(unknown)}")
    if not codes:
        raise ValueError("Не выбран ни один раздел")

    sections: list[SectionPrice] = []
    for code in codes:
        rate = by_code[code]
        if rate.weight is not None and package is not None:
            raw = package * rate.weight * multiplier
        else:
            raw = rate.rate_per_m2 * data.area_m2 * multiplier
        price = max(round_money(raw), rate.min_price)
        sections.append(
            SectionPrice(
                code=code,
                name=rate.name,
                description=rate.description,
                stage=rate.stage,
                price=price,
                cost=(price * rate.cost_share).quantize(Decimal("1")),
            )
        )

    list_total = sum((s.price for s in sections), ZERO)
    direct_cost = sum((s.cost for s in sections), ZERO) + table.trip_cost * data.trips
    free_share = Decimal("1") - table.overhead_share() - table.min_margin
    if free_share <= 0:
        raise ValueError("Налоги, ГИП и минимальная маржа в сумме не могут быть 100% и больше")
    min_price = round_money(direct_cost / free_share, rounding=ROUND_CEILING)
    recommended = max(list_total, min_price)
    if list_total < min_price:
        warnings.append("Цена по прайсу ниже минимально допустимой: рекомендована минимальная цена")
    maximum = round_money(recommended * (1 + table.max_uplift))
    scenarios = {
        name: evaluate_price(table, direct_cost, price, min_price)
        for name, price in (("min", min_price), ("base", recommended), ("max", maximum))
    }
    if table.is_example:
        warnings.append("Используются расценки-пример. Замените их своими в разделе «Расценки».")
    return EstimateResult(
        sections=sections,
        multiplier=multiplier.quantize(Decimal("0.0001")),
        package_price=package,
        list_total=list_total,
        direct_cost=direct_cost,
        min_price=min_price,
        recommended_price=recommended,
        scenarios=scenarios,
        warnings=warnings,
    )


def distribute(sections: list[SectionPrice], total: Decimal, step: Decimal = Decimal("1000")) -> list[Decimal]:
    """Scale section prices to a new total chosen by the owner; amounts are rounded, sum is exact."""
    base = sum((s.price for s in sections), ZERO)
    if base <= 0:
        raise ValueError("Нет разделов для распределения")
    prices = [round_money(s.price * total / base, step) for s in sections]
    largest = max(range(len(prices)), key=lambda i: prices[i])
    prices[largest] += total - sum(prices, ZERO)
    return prices
