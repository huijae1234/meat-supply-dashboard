"""라인 하루 생산 능력: 설정·라인 열, 생산 가능량 제한, 나누기(앞당김→미룸·작업일·원료 도착·납기), 대응 후보·증산 한도, 화면."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import advice as A
from lib import capacity as Q
from lib import company as C
from lib import expiry as E
from lib import planning as P
from lib import response as R
from lib import ui_company as UC

BASE = "2026-01-01"  # 목요일. 1/2 금, 1/5 월 ...


def _settings(cap=1000, shift=3, weekdays=(0, 1, 2, 3, 4)):
    s = C.Settings()
    s.lines = {"햄": "A", "소시지": "A"}
    s.line_capacity = {"A": cap}
    s.shift_days = shift
    s.thaw_weekdays = list(weekdays)
    return s


def _tables(rows, stock=100000, inbound=None):
    """rows = [(날짜, 제품, 수량, 납기, 우선순위)]. 원료는 국산 냉장 앞다리 1.0으로 넉넉하게."""
    inv = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", "냉장", stock]], columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg"])
    plan = pd.DataFrame(rows, columns=["날짜", "제품", "계획물량_kg", "납기", "우선순위"])
    rec = pd.DataFrame([[p, "돼지", "앞다리", "국산", "냉장", 1.0] for p in ("햄", "소시지")], columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    return {"inventory": inv, "inbound": inbound, "plan": plan, "recipe": rec}


def _data(rows, **kw):
    d = C.load_data(_tables(rows, **kw))
    assert d.ready(), d.issues
    return d


def test_settings_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path)
    C.save_settings(_settings(cap=1500, shift=2))
    s = C.load_settings()
    assert s.lines["햄"] == "A" and s.line_capacity["A"] == 1500 and s.shift_days == 2
    bad = _settings()
    bad.line_capacity = {"A": -1}
    with pytest.raises(ValueError):
        C.save_settings(bad)
    bad = _settings(shift=20)
    with pytest.raises(ValueError):
        C.save_settings(bad)


def test_plan_line_column_overrides_mapping():
    raw = _tables([("2026-01-05", "햄", 1500, "2026-01-05", 100)])
    raw["plan"]["라인"] = "B"
    d = C.load_data(raw)
    s = _settings()
    s.line_capacity["B"] = 500
    o = P.simulate(d, settings=s).orders.iloc[0]
    assert o["라인"] == "B" and o["생산가능_kg"] == 500 and "생산 능력(B)" in o["부족원료"]


def test_simulate_limits_by_capacity_without_buying_material():
    d = _data([("2026-01-05", "햄", 800, "2026-01-05", 100), ("2026-01-05", "소시지", 600, "2026-01-05", 100)])
    f = P.simulate(d, settings=_settings())
    assert f.metrics()["능력부족_kg"] == pytest.approx(400) and f.metrics()["생산부족_kg"] == pytest.approx(400)
    assert f.materials["미충족소요_kg"].sum() == pytest.approx(0)                 # 능력 부족을 원료 부족으로 세지 않음
    assert R.spot_candidate(d, f, _settings(), pd.Timestamp(BASE)) is None         # 긴급 현물을 사라고 하지 않음
    assert P.simulate(d).metrics()["능력부족_kg"] == 0                             # 설정이 없으면 제한 없음


def test_level_pulls_earlier_first_then_later():
    # 1/7(수)에 2,500kg: 능력 1,000 -> 앞으로 1/6·1/5·1/2(각 1,000 여유) 먼저
    d = _data([("2026-01-07", "햄", 2500, "2026-01-07", 100)])
    lv = Q.level(d, _settings())
    assert lv.moves["방향"].tolist() == ["앞당김", "앞당김"] and lv.moves["옮긴날짜"].tolist() == [pd.Timestamp("2026-01-06"), pd.Timestamp("2026-01-05")]
    assert lv.overflow.empty
    assert P.simulate(P.change_scenario(d, plan=lv.plan), settings=_settings()).metrics()["능력부족_kg"] == 0


def test_level_skips_weekend_and_marks_late():
    # 1/2(금) 2,000kg: 앞으로는 기준일이라 못 감 -> 1/5(월)로 미룸(주말 건너뜀), 납기 1/2라 납기 넘김
    d = _data([("2026-01-02", "햄", 2000, "2026-01-02", 100)])
    lv = Q.level(d, _settings())
    m = lv.moves.iloc[0]
    assert m["옮긴날짜"] == pd.Timestamp("2026-01-05") and m["방향"] == "미룸" and m["납기상태"] == "납기 넘김"


def test_level_needs_material_arrival_to_pull_earlier():
    # 원료가 1/6에야 들어오면 1/5로는 앞당기지 않는다
    inb = pd.DataFrame([["2026-01-06", "돼지", "앞다리", "국산", "냉장", 5000]], columns=["입고예정일", "육종", "부위", "원산지", "보관", "입고량_kg"])
    d = _data([("2026-01-07", "햄", 2500, "2026-01-09", 100)], stock=0, inbound=inb)
    lv = Q.level(d, _settings())
    assert pd.Timestamp("2026-01-05") not in set(lv.moves["옮긴날짜"])
    assert set(lv.moves["옮긴날짜"]) == {pd.Timestamp("2026-01-06"), pd.Timestamp("2026-01-08")}


def test_level_overflow_and_priority():
    # 모든 날이 꽉 차면 옮길 곳이 없다. 우선순위 숫자가 큰 계획부터 옮긴다
    rows = [(f"2026-01-{d:02d}", "햄", 1000, None, 100) for d in (2, 5, 6, 8, 9, 12)]
    rows.append(("2026-01-07", "햄", 1000, None, 1))
    rows.append(("2026-01-07", "소시지", 500, None, 200))
    d = _data(rows)
    lv = Q.level(d, _settings(shift=1))
    assert lv.overflow["초과_kg"].sum() == pytest.approx(500)
    lv2 = Q.level(d, _settings(cap=1200, shift=1))  # 1/7 1,500kg -> 300kg를 옮겨야 함
    assert set(lv2.moves["제품"]) == {"소시지"}


def test_capacity_candidate_in_response():
    d = _data([("2026-01-07", "햄", 2500, "2026-01-07", 100)])
    base, cands = R.candidates(d, _settings())
    c = next(x for x in cands if x.name == "생산 능력 맞춰 나누기")
    assert P.simulate(c.data, settings=_settings()).metrics()["능력부족_kg"] < base.metrics()["능력부족_kg"]


def test_capped_increase_respects_capacity():
    d = C.load_data(C.sample_tables())
    s = C.Settings()
    s.line_capacity = {"탕류 라인": 1000}
    row = E.analyze(d, s, C.today_kst()).waste.iloc[0]
    capped = E.capped_increase_fix(d, row, s)
    if capped is not None:
        r = E.evaluate(d, capped, s, C.today_kst())
        assert r["생산 부족 변화"] <= 0.5 and r["계획량 변화"] <= 1000


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    monkeypatch.setattr(UC, "market_cached", lambda *args: A.Market())


def test_planning_page_shows_capacity_split(isolated):
    s = C.Settings()
    s.line_capacity = {"햄 라인": 5000}  # 샘플 햄 라인 하루 5,500kg -> 매일 넘침
    C.save_settings(s)
    app = AppTest.from_string("from lib.ui_planning import render_planning\nrender_planning()").run(timeout=60)
    app.button(key="planning_sample").click().run(timeout=60)
    assert not app.exception
    assert any("옮길 곳이 없어" in w.value for w in app.warning)
    assert any("생산 능력" in c.value for c in app.caption)
