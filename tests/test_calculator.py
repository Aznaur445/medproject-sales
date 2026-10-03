from decimal import Decimal as D

import pytest

from app.services.calculator import (
    CurvePoint,
    EstimateInput,
    PriceTableData,
    calculate,
    distribute,
    evaluate_price,
    package_price,
)
from app.services.price_examples import EXAMPLE_PRICE_TABLE

TABLE = PriceTableData.model_validate(EXAMPLE_PRICE_TABLE)
STANDARD = ["AR", "EOM", "VK", "OVIK", "SS"]


def close(value: D, target: D, tolerance: D = D("0.02")) -> bool:
    return abs(value - target) / target <= tolerance


@pytest.mark.parametrize(
    ("area", "target"),
    [
        ("100", "900000"),  # owner: 0.8–1.0 mln
        ("800", "2000000"),  # owner: 2 mln
        ("1500", "3500000"),  # owner: 3.5 mln
    ],
)
def test_standard_package_follows_owner_curve(area, target):
    result = calculate(TABLE, EstimateInput(area_m2=D(area)))
    assert [s.code for s in result.sections] == STANDARD
    assert close(result.list_total, D(target)), result.list_total


def test_price_per_m2_falls_with_area():
    per_m2 = [calculate(TABLE, EstimateInput(area_m2=D(a))).list_total / D(a) for a in ("100", "800", "3000", "8000")]
    assert per_m2 == sorted(per_m2, reverse=True)


def test_standard_weights_sum_to_one():
    assert sum(s.weight for s in TABLE.sections if s.code in STANDARD) == D("1")


def test_curve_interpolation_and_extrapolation():
    curve = [CurvePoint(area_m2=D("100"), price=D("1000")), CurvePoint(area_m2=D("200"), price=D("1500"))]
    assert package_price(curve, D("150")) == D("1250")
    assert package_price(curve, D("300")) == D("2000")  # last slope continues
    assert package_price(curve, D("50")) < D("1000")


def test_scenarios_order_and_margin():
    s = calculate(TABLE, EstimateInput(area_m2=D("800"))).scenarios
    assert s["min"].price <= s["base"].price < s["max"].price
    assert s["min"].margin >= TABLE.min_margin - D("0.01")
    base = s["base"]
    assert base.profit == base.price - base.direct_cost - base.gip_cost - base.other_costs - base.tax
    assert base.tax == (base.price * D("0.07")).quantize(D("1"))


def test_small_area_hits_minimum_prices():
    result = calculate(TABLE, EstimateInput(area_m2=D("20"), sections=STANDARD + ["OOS"]))
    mins = {r.code: r.min_price for r in TABLE.sections}
    assert all(sp.price >= mins[sp.code] for sp in result.sections)


def test_modifiers_increase_price():
    plain = calculate(TABLE, EstimateInput(area_m2=D("1000")))
    hard = calculate(TABLE, EstimateInput(area_m2=D("1000"), modifiers=["clean_rooms", "urgent"]))
    assert hard.list_total > plain.list_total
    assert hard.multiplier == D("1.5000")


def test_legacy_rate_per_m2_still_works():
    legacy = PriceTableData(sections=[{"code": "AR", "name": "АР", "rate_per_m2": D("200"), "min_price": D("0")}])
    assert calculate(legacy, EstimateInput(area_m2=D("1000"))).list_total == D("200000")


def test_unknown_section_or_modifier_rejected():
    with pytest.raises(ValueError):
        calculate(TABLE, EstimateInput(area_m2=D("100"), sections=["XXX"]))
    with pytest.raises(ValueError):
        calculate(TABLE, EstimateInput(area_m2=D("100"), modifiers=["nope"]))


def test_owner_price_below_minimum_flagged():
    result = calculate(TABLE, EstimateInput(area_m2=D("1000")))
    low = evaluate_price(TABLE, result.direct_cost, result.min_price - D("10000"), result.min_price)
    assert low.below_minimum


def test_distribute_keeps_exact_total():
    result = calculate(TABLE, EstimateInput(area_m2=D("1234")))
    prices = distribute(result.sections, D("2345678"))
    assert sum(prices) == D("2345678")
    assert all(p > 0 for p in prices)


def test_example_warning_shown():
    assert any("пример" in w for w in calculate(TABLE, EstimateInput(area_m2=D("500"))).warnings)
