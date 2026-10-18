"""소비기한: 폐기 예상·임박·가정 기한·해동 후 보관 한도, 손실 금액, 대응안(증산·돌리기), 화면 연결, 알림."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import advice as A
from lib import alerts, live
from lib import company as C
from lib import expiry as E
from lib import planning as P
from lib import ui_company as UC

BASE = "2026-01-01"  # 목요일


def _settings(**kw):
    s = C.Settings()
    s.thaw_weekdays = list(range(7))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _tables(stock=1000, expires="2026-01-05", per_day=100, days=10, price=5000, storage="냉장", ready=None, thaw=None):
    """'햄' 하루 per_day kg = 국산 앞다리 1.0. 앞다리 로트 하나."""
    t = pd.Timestamp(BASE)
    inv = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", storage, stock, price, expires, ready, "L1"]],
                       columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "단가_원kg", "사용기한", "사용가능일", "로트"])
    if thaw is not None:
        inv["해동일수"], inv["해동감량률"] = thaw
    plan = pd.DataFrame([[(t + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), "햄", per_day, 10000] for i in range(1, days + 1)],
                        columns=["날짜", "제품", "계획물량_kg", "판매가_원kg"])
    rec = pd.DataFrame([["햄", "돼지", "앞다리", "국산", storage, 1.0]], columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    return {"inventory": inv, "inbound": None, "plan": plan, "recipe": rec}


def _data(**kw):
    d = C.load_data(_tables(**kw))
    assert d.ready(), d.issues
    return d


def test_waste_is_left_at_expiry():
    # 1/2~1/5 4일 x 100kg = 400kg 사용, 600kg 남음
    ex = E.analyze(_data(), _settings(), pd.Timestamp(BASE))
    r = ex.lots.iloc[0]
    assert r["상태"] == "폐기 예상" and r["폐기예상_kg"] == pytest.approx(600) and r["기한까지사용_kg"] == pytest.approx(400)


def test_loss_includes_disposal_cost():
    ex = E.analyze(_data(), _settings(disposal_cost=200.0), pd.Timestamp(BASE))
    assert ex.lots.iloc[0]["손실_원"] == pytest.approx(600 * (5000 + 200))


def test_soon_without_waste():
    ex = E.analyze(_data(stock=300, expires="2026-01-04"), _settings(), pd.Timestamp(BASE))  # 1/4까지 다 씀, 기한 1/4(3일 남음)
    r = ex.lots.iloc[0]
    assert r["폐기예상_kg"] == 0 and r["상태"] == "임박"


def test_assumed_shelf_for_missing_expiry():
    ex = E.analyze(_data(expires=None), _settings(shelf={"냉장": 3, "냉동": 365}), pd.Timestamp(BASE))
    r = ex.lots.iloc[0]
    assert r["기한근거"].startswith("가정") and r["유효기한"] == pd.Timestamp("2026-01-04") and r["폐기예상_kg"] == pytest.approx(700)
    # 가정 기한은 핵심 계산(생산 가능량)에 넣지 않는다
    assert P.simulate(_data(expires=None)).metrics()["생산부족_kg"] == 0


def test_thawed_lot_hold_limit_applies_everywhere():
    d = _data(storage="냉동", expires="2026-06-30", ready="2026-01-02", thaw=(1, 0))
    s = _settings(thaw_hold=1)
    lot = C.material_lots(d, s)[0]
    assert lot["expires"] == pd.Timestamp("2026-01-03")                     # 해동 완료 1/2 + 1일
    ex = E.analyze(d, s, pd.Timestamp(BASE))
    assert ex.lots.iloc[0]["기한근거"].startswith("해동 후 보관 한도") and ex.lots.iloc[0]["폐기예상_kg"] == pytest.approx(800)
    assert P.simulate(d, settings=s).metrics()["생산부족_kg"] == pytest.approx(800)  # 생산 가능량도 같은 규칙


def test_frozen_quantities_shown_in_purchase_weight():
    d = _data(storage="냉동", expires="2026-01-05", ready="2026-01-02", thaw=(1, 4.0))
    ex = E.analyze(d, _settings(thaw_hold=10), pd.Timestamp(BASE))
    r = ex.lots.iloc[0]
    assert r["수량_kg"] == pytest.approx(1000)
    assert r["폐기예상_kg"] == pytest.approx((960 - 400) / 0.96)


def test_hold_status_lot_is_flagged():
    raw = _tables()
    raw["inventory"]["사용상태"] = "검사대기"
    ex = E.analyze(C.load_data(raw), _settings(), pd.Timestamp(BASE))
    assert ex.lots.iloc[0]["상태"].startswith("검사대기") and ex.lots.iloc[0]["폐기예상_kg"] == pytest.approx(1000)


def test_increase_fix_removes_waste():
    d = _data()
    ex = E.analyze(d, _settings(), pd.Timestamp(BASE))
    f = E.increase_fix(d, ex.waste.iloc[0], _settings())
    assert f is not None and (f.data.plan["주문번호"] == "폐기방지-증산").any()
    r = E.evaluate(d, f, _settings(), pd.Timestamp(BASE))
    assert r["로트 폐기 예상 후"] < r["로트 폐기 예상 전"] and r["계획량 변화"] == 600


def test_capped_increase_keeps_other_plans():
    # 사태 폐기를 다 쓰려는 육개장 증산은 양지 부족을 만든다 -> 부족이 늘지 않는 만큼만 늘린 안
    d = C.load_data(C.sample_tables())
    ex = E.analyze(d, today=C.today_kst())
    row = ex.waste[ex.waste["원료"] == C.ingredient_label("소", "사태", "국산", "냉장")].iloc[0]
    full, capped = E.increase_fix(d, row), E.capped_increase_fix(d, row)
    assert E.evaluate(d, full, today=C.today_kst())["생산 부족 변화"] > 0.5
    if capped is not None:  # 날짜에 따라 늘릴 여유가 없을 수도 있다
        r = E.evaluate(d, capped, today=C.today_kst())
        assert r["생산 부족 변화"] <= 0.5 and 0 < r["계획량 변화"] < E.evaluate(d, full, today=C.today_kst())["계획량 변화"]
        assert "한도" in capped.name


def test_capped_increase_none_when_full_is_fine():
    d = _data()
    ex = E.analyze(d, _settings(), pd.Timestamp(BASE))
    assert E.capped_increase_fix(d, ex.waste.iloc[0], _settings()) is None   # 다른 원료가 없어 다 써도 부족 없음


def test_divert_fix_uses_substitute_rule_backwards():
    d = C.load_data(C.sample_tables())
    ex = E.analyze(d, today=C.today_kst())
    row = ex.waste[ex.waste["원료"] == C.ingredient_label("소", "사태", "국산", "냉장")].iloc[0]
    f = E.divert_fix(d, row)
    assert f is not None and f.name.startswith("돌리기: 육개장 (대체")
    r = E.evaluate(d, f, today=C.today_kst())
    assert r["로트 폐기 예상 후"] < r["로트 폐기 예상 전"] and r["계획량 변화"] == 0


def test_alert_for_waste(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path / "live")
    for kind, frame in C.sample_tables().items():
        C.save_raw(kind, frame.to_csv(index=False).encode("utf-8-sig"))
    assert any("폐기 위험" in e["message"] for e in alerts.find_alerts(alerts.CONFIG))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    monkeypatch.setattr(UC, "market_cached", lambda *args: A.Market())


def test_expiry_page_sends_fix_to_planning(isolated):
    app = AppTest.from_string("from lib.ui_expiry import render_expiry\nrender_expiry(1.5, lambda f, *a, **k: f)").run(timeout=60)
    app.button(key="exp_sample").click().run(timeout=60)
    assert not app.exception
    pick = app.selectbox(key="exp_pick")
    name = [o for o in pick.options if o.startswith("돌리기")][0]
    pick.set_value(name).run(timeout=60)
    app.button(key="exp_send").click().run(timeout=60)
    assert app.session_state["planning_seed"] == name
    ext = app.session_state["planning_external"]
    plan_app = AppTest.from_string("from lib.ui_planning import render_planning\nrender_planning()")
    plan_app.session_state["co_source"] = "sample"
    plan_app.session_state["planning_seed"] = name
    plan_app.session_state["planning_external"] = ext
    plan_app.run(timeout=60)
    assert not plan_app.exception
    assert any(t.value == name for t in plan_app.text_input)


def test_today_shows_expiry_line(isolated):
    app = AppTest.from_string("from lib.ui_company import render_today\nrender_today(1.5)").run(timeout=60)
    app.button(key="today_sample").click().run(timeout=60)
    assert not app.exception
    assert any(m.value.startswith("**소비기한**") for m in app.markdown)
