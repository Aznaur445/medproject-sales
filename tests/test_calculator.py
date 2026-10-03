from decimal import Decimal as D

import pytest

from app.services.calculator import EstimateInput, PriceTableData, calculate, distribute, evaluate_price
from app.services.price_examples import EXAMPLE_PRICE_TABLE

TABLE = PriceTableData.model_validate(EXAMPLE_PRICE_TABLE)
MEDSCAN_SECTIONS = ["OBS", "TZ", "AR", "EOM", "VK", "OVIK", "SS", "TX", "POS", "OOS", "PB", "ODI", "PP"]


def test_example_table_reproduces_medscan_proposal():
    """The example rates come from the 9 000 000 ₽ / 5 576.12 m² proposal: totals must match within 1%."""
    result = calculate(TABLE, EstimateInput(area_m2=D("5576.12"), sections=MEDSCAN_SECTIONS))
    assert abs(result.list_total - D("9000000")) / D("9000000") < D("0.01")
    ar = next(s for s in result.sections if s.code == "AR")
    assert ar.price == D("950000")


def test_scenarios_order_and_margin():
    result = calculate(TABLE, EstimateInput(area_m2=D("800")))
    s = result.scenarios
    assert s["min"].price <= s["base"].price < s["max"].price
    assert s["min"].margin >= TABLE.min_margin - D("0.01")
    assert not s["base"].below_minimum
    # profit = price - costs - gip - other - tax
    base = s["base"]
    assert base.profit == base.price - base.direct_cost - base.gip_cost - base.other_costs - base.tax
    assert base.tax == (base.price * D("0.07")).quantize(D("1"))


def test_small_area_hits_minimum_prices():
    result = calculate(TABLE, EstimateInput(area_m2=D("50")))
    assert all(sp.price >= next(r.min_price for r in TABLE.sections if r.code == sp.code) for sp in result.sections)


def test_modifiers_increase_price():
    plain = calculate(TABLE, EstimateInput(area_m2=D("1000")))
    hard = calculate(TABLE, EstimateInput(area_m2=D("1000"), modifiers=["clean_rooms", "urgent"]))
    assert hard.list_total > plain.list_total
    assert hard.multiplier == D("1.5000")


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
