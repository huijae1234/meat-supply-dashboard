"""원가 영향: 단가·판매가 검증, 제품 kg당 원료비, 단가 누락 처리, 변동 가정, 발주 금액, 화면."""
import math

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import advice as A
from lib import company as C
from lib import cost as K
from lib import ui_company as UC
from lib.plain import won, won_big

BASE = "2026-01-01"


def _tables(inv_price=(5000,), inb=None, sale=10000, second=False):
    """제품 '햄' 하루 100kg, 앞다리 0.6 + (second면) 뒷다리 0.4."""
    t = pd.Timestamp(BASE)
    inv_rows = [[BASE, "돼지", "앞다리", "국산", "냉장", 1000, p] for p in inv_price]
    if second:
        inv_rows.append([BASE, "돼지", "뒷다리", "수입", "냉동", 5000, None])
    inv = pd.DataFrame(inv_rows, columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "단가_원kg"])
    plan = pd.DataFrame([[(t + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), "햄", 100, sale] for i in range(1, 11)],
                        columns=["날짜", "제품", "계획물량_kg", "판매가_원kg"])
    rec = [["햄", "돼지", "앞다리", "국산", "냉장", 0.6]]
    if second:
        rec.append(["햄", "돼지", "뒷다리", "수입", "냉동", 0.4])
    recipe = pd.DataFrame(rec, columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    inbound = None
    if inb:
        inbound = pd.DataFrame([[d, "돼지", "앞다리", "국산", "냉장", q, p] for d, q, p in inb],
                               columns=["입고예정일", "육종", "부위", "원산지", "보관", "입고량_kg", "단가_원kg"])
    return {"inventory": inv, "inbound": inbound, "plan": plan, "recipe": recipe}


def _data(**kw):
    d = C.load_data(_tables(**kw))
    assert d.ready(), d.issues
    return d


def test_price_columns_optional_and_validated():
    raw = _tables()
    raw["inventory"]["단가_원kg"] = ["5,000원"]
    d = C.load_data(raw)
    assert d.ready() and d.inventory["단가_원kg"].iloc[0] == 5000
    raw["inventory"]["단가_원kg"] = [-1]
    assert not C.load_data(raw).ready()
    raw["inventory"]["단가_원kg"] = ["abc"]
    assert not C.load_data(raw).ready()
    raw["inventory"] = raw["inventory"].drop(columns="단가_원kg")
    d = C.load_data(raw)
    assert d.ready() and not K.has_prices(d)


def test_alias_header_is_recognized():
    raw = _tables()
    raw["inventory"] = raw["inventory"].rename(columns={"단가_원kg": "매입단가"})
    raw["plan"] = raw["plan"].rename(columns={"판매가_원kg": "납품가"})
    d = C.load_data(raw)
    assert K.has_prices(d) and K.has_sales(d)


def test_product_cost_and_ratio():
    row = K.product_costs(_data()).iloc[0]
    assert row["원료비_원kg"] == pytest.approx(3000)          # 0.6 x 5000
    assert row["원료비율"] == pytest.approx(30)
    assert row["계획원료비"] == pytest.approx(3000 * 1000)     # 10일 x 100kg


def test_weighted_average_and_next_price():
    d = _data(inb=[("2026-01-05", 1000, 6000)])
    p = K.material_prices(d).iloc[0]
    assert p["평균단가"] == pytest.approx(5500)                 # (1000x5000 + 1000x6000) / 2000
    assert p["입고예정단가"] == 6000
    assert K.product_costs(d, "next").iloc[0]["원료비_원kg"] == pytest.approx(3600)


def test_missing_price_is_not_zero():
    row = K.product_costs(_data(second=True)).iloc[0]
    assert math.isnan(row["원료비_원kg"])
    assert "뒷다리" in row["단가없는원료"]


def test_partial_price_coverage_reported():
    raw = _tables(inv_price=(5000, None))
    d = C.load_data(raw)
    p = K.material_prices(d).iloc[0]
    assert p["평균단가"] == 5000 and p["단가입력비율"] == pytest.approx(0.5)


def test_change_assumption_applies():
    d = _data()
    label = C.ingredient_label("돼지", "앞다리", "국산", "냉장")
    row = K.product_costs(d, "avg", {label: 10}).iloc[0]
    assert row["가정원료비_원kg"] == pytest.approx(3300)
    assert row["변화율"] == pytest.approx(10)
    assert row["가정원료비율"] == pytest.approx(33)


def test_order_amount_uses_order_qty():
    d = _data(inv_price=(5000,))
    proj = C.project(d, today=pd.Timestamp(BASE))
    amt = K.material_amounts(d, proj).iloc[0]
    assert amt["발주금액"] == pytest.approx(amt["발주검토량_kg"] * 5000)


def test_market_changes_without_market():
    assert K.market_changes(_data(), A.Market()) == {}


def _daily(values, start="2026-01-01"):
    return pd.date_range(start, periods=len(values), freq="D")


def test_auction_change_smoothed_and_domestic_only():
    dates = _daily([0] * 40)
    beef = pd.DataFrame({"date": dates, "cut": "양지", "han_krw_kg": [10000] * 20 + [11000] * 20})
    pig = pd.DataFrame({"date": dates, "grade": "평균", "price_krw_kg": [5000] * 30 + [5500] * 10})
    d = C.load_data(C.sample_tables())
    out = K.auction_changes(d, beef, pig)
    pork = C.ingredient_label("돼지", "앞다리", "국산", "냉장")
    assert out[pork][0] == pytest.approx(10.0)                     # 최근 5일 5500 vs 30일 전 5일 5000
    assert C.ingredient_label("소", "양지", "수입", "냉동") not in out   # 수입육은 연결하지 않음
    assert C.ingredient_label("돼지", "뒷다리", "수입", "냉동") not in out


def test_auction_change_needs_enough_history():
    beef = pd.DataFrame({"date": _daily([0] * 8), "cut": "사태", "han_krw_kg": [1] * 8})
    assert K.auction_changes(C.load_data(C.sample_tables()), beef, pd.DataFrame(columns=["date", "grade", "price_krw_kg"])) == {}


def test_sample_has_prices_and_all_products_costed():
    d = C.load_data(C.sample_tables())
    prod = K.product_costs(d)
    assert K.has_prices(d) and K.has_sales(d)
    assert prod["원료비_원kg"].notna().all()


def test_money_format():
    assert won(4373.2) == "4,373원"
    assert won_big(163_800_000) == "1.64억원"
    assert won_big(-2_830_000) == "-283만원"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    monkeypatch.setattr(UC, "market_cached", lambda *args: A.Market())


def test_cost_page_sample(isolated):
    app = AppTest.from_string("from lib.ui_cost import render_cost\nrender_cost(1.5, lambda f, *a, **k: f)").run(timeout=30)
    app.button(key="cost_sample").click().run(timeout=30)
    assert not app.exception
    assert len(app.dataframe) >= 3
    assert not C.COMPANY_DIR.exists()


def test_today_shows_cost_line(isolated):
    app = AppTest.from_string("from lib.ui_company import render_today\nrender_today(1.5)").run(timeout=30)
    app.button(key="today_sample").click().run(timeout=30)
    assert not app.exception
    assert any("계획 기간 원료비" in m.value for m in app.markdown)
