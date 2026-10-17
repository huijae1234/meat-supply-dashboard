"""대응 후보: 대체표 검증, 대체 배합·원산지 경고, 긴급 현물, 생산일 늦추기, 공헌이익 손실·위약금, 화면."""
import math

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import advice as A
from lib import company as C
from lib import planning as P
from lib import response as R
from lib import ui_company as UC

BASE = "2026-01-01"  # 목요일


def _settings():
    s = C.Settings()
    s.thaw_weekdays = list(range(7))
    s.spot_lead = {"국산": 1, "수입": 3}
    s.spot_premium = 20.0
    return s


def _tables(stock=300, other=5000, per_day=100, days=6, sub=True, sale=10000, penalty=None):
    """'햄' 하루 per_day kg = 국산 냉장 앞다리 1.0. 대체: 수입 냉장 앞다리 최대 50%."""
    t = pd.Timestamp(BASE)
    inv = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", "냉장", stock, 5000],
                        [BASE, "돼지", "앞다리", "수입", "냉장", other, 4000]],
                       columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "단가_원kg"])
    plan = pd.DataFrame([[(t + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), "햄", per_day, sale, (t + pd.Timedelta(days=i)).strftime("%Y-%m-%d")]
                         for i in range(1, days + 1)], columns=["날짜", "제품", "계획물량_kg", "판매가_원kg", "납기"])
    if penalty is not None:
        plan["위약금_원kg"] = penalty
    rec = pd.DataFrame([["햄", "돼지", "앞다리", "국산", "냉장", 1.0]], columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    subs = pd.DataFrame([["햄", "돼지", "앞다리", "국산", "냉장", "앞다리", "수입", "냉장", 50, 1.0]],
                        columns=["제품", "육종", "부위", "원산지", "보관", "대체부위", "대체원산지", "대체보관", "최대대체비율", "환산계수"]) if sub else None
    return {"inventory": inv, "inbound": None, "plan": plan, "recipe": rec, "substitute": subs}


def _data(**kw):
    d = C.load_data(_tables(**kw))
    assert d.ready(), d.issues
    return d


def test_substitute_table_validation():
    raw = _tables()
    raw["substitute"]["최대대체비율"] = 150
    assert not C.load_data(raw).ready()
    raw = _tables()
    raw["substitute"]["대체부위"] = "없는부위"
    assert not C.load_data(raw).ready()
    raw = _tables()
    raw["substitute"]["대체원산지"] = "국산"          # 원래와 같은 원료
    assert not C.load_data(raw).ready()
    raw = _tables()
    raw["substitute"]["제품"] = "전체"
    assert C.load_data(raw).substitute["제품"].iloc[0] == "*"


def test_unused_rule_is_noted():
    raw = _tables()
    raw["substitute"]["제품"] = "없는제품"
    d = C.load_data(raw)
    assert d.ready() and any(i.file == "대체표" and "쓰이지 않는" in i.message for i in d.issues)


def test_substitute_candidate_splits_recipe_and_warns_origin():
    d = _data(stock=450)  # 국산 450kg: 5일째 50kg 부족, 6일째 100kg 부족
    base = P.simulate(d, settings=_settings())
    c = R.substitute_candidate(d, base, _settings())
    # 5일째: 50kg만 만들 수 있었음 -> 50% 대체, 6일째: 0kg -> 필요 100%지만 최대 50%
    assert set(c.data.plan["제품"]) == {"햄", "햄 (대체 50%)"}       # 부족한 날만 바뀜
    rec = c.data.recipe[c.data.recipe["제품"] == "햄 (대체 50%)"].set_index("원산지")["투입량_kg"]
    assert rec["국산"] == pytest.approx(0.5) and rec["수입"] == pytest.approx(0.5)
    assert any("원산지 변경" in w for w in c.warnings)
    assert any("최대 비율" in n for n in c.notes)
    after = P.simulate(c.data, settings=_settings())
    assert after.metrics()["생산부족_kg"] < base.metrics()["생산부족_kg"]
    assert R.extra_cost(d, c.data, _settings()) < 0                      # 수입이 더 싸서 원료비가 줄어듦


def test_substitute_rate_only_as_much_as_short():
    # 국산 520kg, 하루 100kg 6일: 6일째 20kg만 만들 수 있음 -> 필요 80% 중 최대 50%
    # 최대 비율 100%로 바꾸면 80%만 대체
    raw = _tables(stock=520)
    raw["substitute"]["최대대체비율"] = 100
    d = C.load_data(raw)
    c = R.substitute_candidate(d, P.simulate(d, settings=_settings()), _settings())
    assert "햄 (대체 80%)" in set(c.data.plan["제품"])


def test_partial_substitute_cannot_help_when_original_is_gone():
    # 최대 50% 대체는 나머지 50%의 원래 원료가 있어야 한다
    d = _data(stock=300)
    base = P.simulate(d, settings=_settings())
    c = R.substitute_candidate(d, base, _settings())
    assert P.simulate(c.data, settings=_settings()).metrics()["생산부족_kg"] == pytest.approx(base.metrics()["생산부족_kg"])


def test_spot_candidate_arrives_after_lead_and_adds_premium():
    d = _data(sub=False)
    base = P.simulate(d, settings=_settings())
    c = R.spot_candidate(d, base, _settings(), pd.Timestamp(BASE))
    row = c.data.inbound.iloc[-1]
    assert row["입고예정일"] == pd.Timestamp("2026-01-02") and row["공급처"] == "긴급 현물(가정)"
    assert R.extra_cost(d, c.data, _settings()) == pytest.approx(row["입고량_kg"] * 5000 * 0.20)
    assert P.simulate(c.data, settings=_settings()).metrics()["생산부족_kg"] == pytest.approx(0, abs=1e-6)


def test_delay_candidate_moves_to_next_ready_and_flags_due():
    d = _data(sub=False, stock=200)
    inb = pd.DataFrame([["2026-01-05", "돼지", "앞다리", "국산", "냉장", 2000]], columns=["입고예정일", "육종", "부위", "원산지", "보관", "입고량_kg"])
    raw = _tables(sub=False, stock=200)
    raw["inbound"] = inb
    d = C.load_data(raw)
    base = P.simulate(d, settings=_settings())
    c = R.delay_candidate(d, base, _settings())
    assert (c.data.plan["날짜"] == pd.Timestamp("2026-01-05")).sum() >= 2
    assert any("납기 넘김" in n for n in c.notes)


def test_money_contribution_and_penalty():
    d = _data(sub=False, penalty=1000)
    base = P.simulate(d, settings=_settings())
    m = R.money(d, base, _settings())
    short = base.metrics()["생산부족_kg"]
    assert m["감산손실"] == pytest.approx(short * (10000 - 5000))
    assert m["위약금"] == pytest.approx(short * 1000)


def test_extra_cost_for_edited_scenario():
    d = _data(sub=False)
    inb = pd.DataFrame([["2026-01-03", "돼지", "앞다리", "국산", "냉장", 500, 6000]],
                       columns=["입고예정일", "육종", "부위", "원산지", "보관", "입고량_kg", "단가_원kg"])
    edited = P.change_scenario(d, inbound=inb)
    assert R.extra_cost(d, edited) == pytest.approx(500 * (6000 - 5000))   # 기준 단가 5000원 대비 할증분
    no_price = P.change_scenario(d, inbound=inb.drop(columns="단가_원kg"))
    assert R.extra_cost(d, no_price) == pytest.approx(0)                     # 단가 미입력은 할증 0
    moved = P.change_scenario(d, plan=d.plan.assign(날짜=d.plan["날짜"] + pd.Timedelta(days=1)))
    assert R.extra_cost(d, moved) == pytest.approx(0)


def test_money_unknown_without_sales():
    raw = _tables(sub=False)
    raw["plan"] = raw["plan"].drop(columns="판매가_원kg")
    d = C.load_data(raw)
    assert math.isnan(R.money(d, P.simulate(d), _settings())["감산손실"])


def test_candidates_and_compare_on_sample():
    d = C.load_data(C.sample_tables())
    base, cands = R.candidates(d, today=C.today_kst())
    names = [c.name for c in cands]
    assert names[0] == "감산 수용" and "대체 원료" in names and "긴급 현물 구매" in names
    table = R.compare(d, cands)
    assert table.loc[table["대응안"] == "감산 수용", "추가비용"].iloc[0] == 0
    assert table["생산부족_kg"].min() <= table.loc[table["대응안"] == "감산 수용", "생산부족_kg"].iloc[0]


def test_no_shortage_gives_only_baseline():
    d = _data(stock=5000)
    _, cands = R.candidates(d, _settings())
    assert [c.name for c in cands] == ["감산 수용"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    monkeypatch.setattr(UC, "market_cached", lambda *args: A.Market())


def test_planning_loads_candidate_into_editor(isolated):
    app = AppTest.from_string("from lib.ui_planning import render_planning\nrender_planning()").run(timeout=60)
    app.button(key="planning_sample").click().run(timeout=60)
    assert not app.exception
    app.selectbox[0].set_value("긴급 현물 구매").run(timeout=60)
    assert not app.exception
    assert app.session_state["planning_seed"] == "긴급 현물 구매"
    assert any(t.value == "긴급 현물 구매" for t in app.text_input)
    assert not C.COMPANY_DIR.exists()
