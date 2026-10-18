"""예비 보완 2차 묶음: 생산일 늦추기 원료 전체 확인, 샘플 예시 능력, 부산물 순원료비, 재고 입고일, 발주서 USD, 원가 가정 저장."""
import io

import openpyxl
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import advice as A
from lib import company as C
from lib import cost as K
from lib import expiry as E
from lib import fx, live
from lib import orders as O
from lib import planning as P
from lib import response as R
from lib import ui_company as UC

BASE = "2026-01-01"  # 목요일


# ---------------------------------------------------------------- 1. 생산일 늦추기: 원료 전체 확인
def _two_materials(b_expires="2026-03-01"):
    """'햄' = 앞다리(A) 0.5 + 뒷다리(B) 0.5. A는 1/2에 바닥, 1/7에 입고. B는 넉넉하지만 b_expires에 기한."""
    t = pd.Timestamp(BASE)
    inv = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", "냉장", 50, None], [BASE, "돼지", "뒷다리", "국산", "냉장", 5000, b_expires]],
                       columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "사용기한"])
    inb = pd.DataFrame([["2026-01-07", "돼지", "앞다리", "국산", "냉장", 5000]], columns=["입고예정일", "육종", "부위", "원산지", "보관", "입고량_kg"])
    plan = pd.DataFrame([["2026-01-05", "햄", 200]], columns=["날짜", "제품", "계획물량_kg"])
    rec = pd.DataFrame([["햄", "돼지", "앞다리", "국산", "냉장", 0.5], ["햄", "돼지", "뒷다리", "국산", "냉장", 0.5]],
                       columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    d = C.load_data({"inventory": inv, "inbound": inb, "plan": plan, "recipe": rec})
    assert d.ready(), d.issues
    return d


def test_delay_moves_to_day_with_all_materials():
    d = _two_materials()
    c = R.delay_candidate(d, P.simulate(d), C.Settings())
    assert c.data.plan["날짜"].iloc[0] == pd.Timestamp("2026-01-07")        # 앞다리가 들어오는 날, 뒷다리도 있음
    assert P.simulate(c.data).metrics()["생산부족_kg"] == 0


def test_delay_does_not_move_when_other_material_expired():
    d = _two_materials(b_expires="2026-01-06")                                 # 앞다리 입고(1/7) 전에 뒷다리 기한
    assert R.delay_candidate(d, P.simulate(d), C.Settings()) is None


# ---------------------------------------------------------------- 2. 샘플 예시 능력
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    monkeypatch.setattr(UC, "market_cached", lambda *args: A.Market())


def test_sample_capacity_toggle_only_in_session(isolated):
    app = AppTest.from_string("from lib.ui_planning import render_planning\nrender_planning()").run(timeout=60)
    app.button(key="planning_sample").click().run(timeout=60)
    app.button(key="sample_capacity_btn").click().run(timeout=60)
    assert not app.exception and app.session_state["sample_capacity"]
    assert any("샘플 예시 능력이 켜져" in c.value for c in app.caption)
    assert any(b.label.startswith("조정된 생산계획") for b in app.get("download_button"))
    assert not (C.COMPANY_DIR / "settings.json").exists()                      # 설정 파일에 저장하지 않음


def test_sample_has_rush_order_over_capacity():
    d = C.load_data(C.sample_tables())
    s = C.Settings()
    s.line_capacity = dict(C.SAMPLE_CAPACITY)
    from lib import capacity as Q
    lv = Q.level(d, s)
    assert (d.plan["주문번호"] == "DEMO-RUSH").any() and len(lv.moves) and lv.overflow.empty


# ---------------------------------------------------------------- 3. 부산물
def test_byproduct_net_cost_and_validation():
    d = C.load_data(C.sample_tables())
    pc = K.product_costs(d).set_index("제품")
    assert pc.loc["프레스햄", "부산물회수_원kg"] == pytest.approx(0.55 * 0.06 * 800 + 0.30 * (0.08 * 800 + 0.05 * 300))
    assert pc.loc["프레스햄", "원료비_원kg"] == pytest.approx(pc.loc["프레스햄", "총원료비_원kg"] - pc.loc["프레스햄", "부산물회수_원kg"])
    assert pc.loc["베이컨", "부산물회수_원kg"] == 0
    raw = C.sample_tables()
    raw["byproduct"].loc[0, "부산물률"] = 120
    assert not C.load_data(raw).ready()
    raw = C.sample_tables()
    raw.pop("byproduct")
    d2 = C.load_data(raw)
    pc2 = K.product_costs(d2).set_index("제품")
    assert (pc2["원료비_원kg"] == pc2["총원료비_원kg"]).all()


def test_byproduct_not_scaled_by_assumption():
    d = C.load_data(C.sample_tables())
    label = C.ingredient_label("돼지", "앞다리", "국산", "냉장")
    a = K.product_costs(d).set_index("제품").loc["프레스햄"]
    b = K.product_costs(d, "avg", {label: 10}).set_index("제품").loc["프레스햄"]
    assert b["부산물회수_원kg"] == pytest.approx(a["부산물회수_원kg"])


# ---------------------------------------------------------------- 4. 재고 입고일
def _one_lot(received=None):
    inv = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", "냉장", 1000, received]], columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "입고일"])
    plan = pd.DataFrame([["2026-01-02", "햄", 10]], columns=["날짜", "제품", "계획물량_kg"])
    rec = pd.DataFrame([["햄", "돼지", "앞다리", "국산", "냉장", 1.0]], columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    return {"inventory": inv, "inbound": None, "plan": plan, "recipe": rec}


def test_assumed_shelf_counts_from_received_date():
    d = C.load_data(_one_lot("2025-12-25"))
    ex = E.analyze(d, C.Settings(), pd.Timestamp(BASE))
    r = ex.lots.iloc[0]
    assert r["유효기한"] == pd.Timestamp("2026-01-04") and "입고일부터" in r["기한근거"]   # 12/25 + 냉장 10일
    d0 = C.load_data(_one_lot(None))
    assert "기준일부터" in E.analyze(d0, C.Settings(), pd.Timestamp(BASE)).lots.iloc[0]["기한근거"]
    assert not C.load_data(_one_lot("2026-01-05")).ready()                       # 기준일보다 늦은 입고일


# ---------------------------------------------------------------- 5. 발주서 USD
class _S:
    def get(self, url, params, timeout):
        class Rsp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"result": 1, "cur_unit": "USD", "deal_bas_r": "1,400.00"}]
        return Rsp()


def test_order_usd_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setenv(fx.KEY_ENV, "k")
    fx.collect(3, today=pd.Timestamp("2026-01-02"), session=_S())
    raw = C.sample_tables()
    raw["inbound"]["단가_USDkg"] = [None, 7.0]                                   # 양지 입고에 USD 단가
    d = C.load_data(raw)
    o = O.draft(d, C.project(d, today=C.today_kst())).set_index("원료")
    yangji = C.ingredient_label("소", "양지", "수입", "냉동")
    hind = C.ingredient_label("돼지", "뒷다리", "수입", "냉동")
    assert o.loc[yangji, "단가_USDkg"] == 7.0                                     # 입고 예정의 USD 단가 우선
    assert o.loc[hind, "단가_USDkg"] == pytest.approx(o.loc[hind, "단가_원kg"] / 1400)  # 없으면 원화 ÷ 환율
    assert o[o["원산지"] == "국산"]["단가_USDkg"].isna().all()
    wb = openpyxl.load_workbook(io.BytesIO(O.workbook(o.reset_index(), pd.DataFrame(), {"환율일": "2026-01-02"})))
    ws = wb["발주_B무역"]
    cells = [c.value for r in ws.iter_rows() for c in r if c.value is not None]
    assert "금액(USD)" in cells and any(str(v).startswith("USD 1,400.00원") for v in cells)


# ---------------------------------------------------------------- 6. 원가 가정 저장
def test_assumption_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path)
    K.save_assumption({"이름": "A", "가정": {"x": 1.0}, "계획기간원료비_현재": 1, "계획기간원료비_가정": 2})
    K.save_assumption({"이름": "B", "가정": {}, "계획기간원료비_현재": 1, "계획기간원료비_가정": 1})
    K.save_assumption({"이름": "A", "가정": {"x": 5.0}, "계획기간원료비_현재": 1, "계획기간원료비_가정": 3})
    items = K.load_assumptions()
    assert [e["이름"] for e in items] == ["A", "B"] and items[0]["가정"]["x"] == 5.0
    with pytest.raises(ValueError):
        K.save_assumption({"이름": " "})


def test_cost_page_save_load_and_link(isolated):
    app = AppTest.from_string("from lib.ui_cost import render_cost\nrender_cost(1.5, lambda f, *a, **k: f)").run(timeout=60)
    app.button(key="cost_sample").click().run(timeout=60)
    app.session_state["cost_pct"] = {C.ingredient_label("돼지", "앞다리", "국산", "냉장"): 10.0}
    app.run(timeout=60)
    app.text_input(key="cost_assume_name").set_value("회의안").run(timeout=60)
    app.button(key="cost_assume_save").click().run(timeout=60)
    assert not app.exception and app.session_state["sample_assumptions"][0]["이름"] == "회의안"
    assert not C.COMPANY_DIR.exists()                                            # 샘플은 디스크에 쓰지 않음
    app.button(key="cost_assume_link").click().run(timeout=60)
    ctx = app.session_state["pending_action"]
    assert ctx["원가가정"]["이름"] == "회의안"
    log = AppTest.from_string("import streamlit as st\nst.session_state['co_source']='sample'\n"
                              f"st.session_state['pending_action']={dict(ctx)!r}\n"
                              "from lib.ui_planning import render_worklog\nrender_worklog()").run(timeout=60)
    assert not log.exception and any(t.value.startswith("[원가 가정] 회의안") for t in log.text_area)
