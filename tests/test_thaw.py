"""해동 계획: 해동일수·감량률 반영, 이미 해동 중인 로트, 해동 시간 부족, 해동실 용량, 설정·화면."""
import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import advice as A
from lib import company as C
from lib import planning as P
from lib import thaw as T
from lib import ui_company as UC

BASE = "2026-01-01"
KEY = ("돼지", "뒷다리", "수입", "냉동")
LABEL = C.ingredient_label(*KEY)


def _tables(lots=((10000, None),), per_day=1000, days=10, inbound=None, thaw_cols=None):
    """제품 '햄' 하루 per_day kg, 수입 냉동 뒷다리 1.0. lots = [(수량, 사용가능일)]."""
    t = pd.Timestamp(BASE)
    inv = pd.DataFrame([[BASE, *KEY, q, f"ST-{i}", ready, 14, 2, 14] for i, (q, ready) in enumerate(lots)],
                       columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "로트", "사용가능일", "안전재고일수", "리드타임일수", "발주주기일"])
    if thaw_cols:
        for c, v in thaw_cols.items():
            inv[c] = v
    plan = pd.DataFrame([[(t + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), "햄", per_day] for i in range(1, days + 1)],
                        columns=["날짜", "제품", "계획물량_kg"])
    rec = pd.DataFrame([["햄", *KEY, 1.0]], columns=["제품", "육종", "부위", "원산지", "보관", "투입량_kg"])
    inb = None
    if inbound:
        inb = pd.DataFrame([[d, *KEY, q] for d, q in inbound], columns=["입고예정일", "육종", "부위", "원산지", "보관", "입고량_kg"])
    return {"inventory": inv, "inbound": inb, "plan": plan, "recipe": rec}


def _data(**kw):
    d = C.load_data(_tables(**kw))
    assert d.ready(), d.issues
    return d


def _settings(days=2, loss=0.0, cap=0, weekdays=(0, 1, 2, 3, 4, 5, 6), hold=2, holidays=()):
    """기본은 모든 요일 해동 가능(작업일 규칙은 따로 시험)."""
    s = C.Settings()
    s.thaw_days = {"국산": days, "수입": days}
    s.thaw_loss = {"국산": loss, "수입": loss}
    s.thaw_capacity = cap
    s.thaw_weekdays = list(weekdays)
    s.thaw_hold = hold
    s.holidays = list(holidays)
    return s


def test_frozen_lot_without_ready_date_waits_for_thaw():
    d = _data()
    assert C.Settings().thaw_weekdays == [0, 1, 2, 3, 4]       # 기본은 월~금
    lots = C.material_lots(d, _settings(days=2))
    assert lots[0]["ready"] == pd.Timestamp("2026-01-04")       # 기준일 다음 날 해동 시작 + 2일
    orders = P.simulate(d, settings=_settings(days=2)).orders
    assert orders["생산가능_kg"].tolist()[:4] == [0, 0, 1000, 1000]  # 첫 이틀은 해동이 안 끝나 생산 불가


def test_thawing_lot_is_used_first_without_new_event():
    d = _data(lots=((2000, "2026-01-02"), (10000, None)))
    tp = T.plan(d, _settings(days=2))
    assert tp.problems.empty
    assert tp.events["사용일"].min() == pd.Timestamp("2026-01-04")     # 1/2·1/3은 해동 중 로트로
    first = tp.events.iloc[0]
    assert first["해동시작일"] == pd.Timestamp("2026-01-02") and first["로트"] == "ST-1"


def test_loss_increases_frozen_input_and_order():
    d = _data(lots=((2000, "2026-01-02"), (10000, None)))
    tp = T.plan(d, _settings(days=2, loss=4.0))
    assert tp.events.iloc[0]["냉동투입_kg"] == pytest.approx(1000 / 0.96)
    lot = [l for l in C.material_lots(d, _settings(loss=4.0)) if l["id"] == "ST-0"][0]
    assert lot["qty"] == pytest.approx(1920)
    no_loss = C.project(d, settings=_settings(loss=0.0)).summary.iloc[0]
    with_loss = C.project(d, settings=_settings(loss=4.0)).summary.iloc[0]
    assert with_loss["발주량_kg"] > no_loss["발주량_kg"]


def test_csv_thaw_values_override_defaults():
    d = _data(thaw_cols={"해동일수": 1, "해동감량률": 5})
    lot = C.material_lots(d, _settings(days=3, loss=1.0))[0]
    assert lot["thaw_days"] == 1 and lot["loss"] == 5 and lot["ready"] == pd.Timestamp("2026-01-03")


def test_thaw_time_shortage_reported():
    tp = T.plan(_data(), _settings(days=2))
    early = tp.problems[tp.problems["날짜"] <= pd.Timestamp("2026-01-03")]
    assert len(early) == 2 and early["이유"].str.startswith("해동 시간 부족").all()


def test_stock_shortage_reported():
    tp = T.plan(_data(lots=((3000, "2026-01-02"),), days=5), _settings(days=1))
    assert tp.problems["이유"].str.startswith("냉동 재고 부족").all()
    assert tp.problems["부족_kg"].sum() == pytest.approx(2000)


def test_capacity_moves_load_earlier():
    d = _data(lots=((2000, "2026-01-02"), (20000, None)), per_day=1000)
    s = _settings(days=1, cap=1500)
    d.plan.loc[d.plan["날짜"] == pd.Timestamp("2026-01-06"), "계획물량_kg"] = 2500
    tp = T.plan(d, s)
    load = tp.load().groupby("해동시작일")["냉동투입_kg"].sum()
    assert (load <= 1500 + 1e-6).all()
    assert (tp.events["상태"] == "용량 때문에 앞당김").any()
    assert tp.events["해동후_kg"].sum() == pytest.approx(10000 - 2000 + 1500)


def test_capacity_overflow_when_cannot_move():
    d = _data(lots=((0.001, "2026-01-02"), (20000, None)), per_day=3000, days=3)
    tp = T.plan(d, _settings(days=1, cap=2000))
    assert tp.problems["이유"].str.startswith("해동실 용량 초과").any()


def test_weekend_moves_start_to_friday_with_wait():
    # 1/1(목) 기준. 1/5(월)에 쓸 물량은 1일 해동이면 1/4(일)에 넣어야 하지만 주말이라 1/2(금)에 넣고 2일 대기
    d = _data(lots=((0.001, "2026-01-02"), (20000, None)), days=5)
    tp = T.plan(d, _settings(days=1, weekdays=range(5), hold=2))
    mon = tp.events[tp.events["사용일"] == pd.Timestamp("2026-01-05")].iloc[0]
    assert mon["해동시작일"] == pd.Timestamp("2026-01-02") and mon["대기일수"] == 2
    assert mon["상태"] == "주말·휴무일 때문에 앞당김"


def test_hold_limit_blocks_too_early_thaw():
    d = _data(lots=((0.001, "2026-01-02"), (20000, None)), days=5)
    tp = T.plan(d, _settings(days=1, weekdays=range(5), hold=1))
    mon = tp.problems[tp.problems["날짜"] == pd.Timestamp("2026-01-05")]
    assert len(mon) and mon["이유"].iloc[0].startswith("해동 후 보관 한도 초과")


def test_holiday_is_skipped_and_core_ready_date_follows():
    s = _settings(days=1, weekdays=range(5), holidays=["2026-01-02"])
    d = _data()
    lot = C.material_lots(d, s)[0]
    assert lot["ready"] == pd.Timestamp("2026-01-06")              # 1/2 휴무, 1/3·1/4 주말 -> 1/5(월) 투입 -> 1/6
    tp = T.plan(d, s)
    assert not (tp.events["해동시작일"] == pd.Timestamp("2026-01-02")).any()


def test_settings_validation_for_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path)
    with pytest.raises(ValueError):
        C.save_settings(_settings(weekdays=()))
    with pytest.raises(ValueError):
        C.save_settings(_settings(holidays=["2026/01/02"]))
    with pytest.raises(ValueError):
        C.save_settings(_settings(hold=30))


def test_validation_of_thaw_columns():
    raw = _tables(thaw_cols={"해동일수": 20})
    assert not C.load_data(raw).ready()
    raw = _tables(thaw_cols={"해동감량률": 60})
    assert not C.load_data(raw).ready()


def test_settings_roundtrip_and_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path)
    C.save_settings(_settings(days=3, loss=2.5, cap=4000))
    s = C.load_settings()
    assert s.thaw_days["수입"] == 3 and s.thaw_loss["국산"] == 2.5 and s.thaw_capacity == 4000
    old = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    for k in ("thaw_days", "thaw_loss", "thaw_capacity"):
        old.pop(k)
    (tmp_path / "settings.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    assert C.load_settings().thaw_days == C.DEFAULT_THAW_DAYS
    bad = _settings(days=20)
    with pytest.raises(ValueError):
        C.save_settings(bad)


def test_sample_has_thawing_and_frozen_lots():
    d = C.load_data(C.sample_tables())
    assert not any(i.level in ("오류", "경고") and "중복" in i.message for i in d.issues)
    tp = T.plan(d)
    assert len(tp.events) and (tp.events["원료"] == C.ingredient_label("돼지", "뒷다리", "수입", "냉동")).any()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    monkeypatch.setattr(UC, "market_cached", lambda *args: A.Market())


def test_thaw_page_sample(isolated):
    app = AppTest.from_string("from lib.ui_thaw import render_thaw\nrender_thaw(1.5, lambda f, *a, **k: f)").run(timeout=30)
    app.button(key="thaw_sample").click().run(timeout=30)
    assert not app.exception
    assert len(app.dataframe) >= 2
    assert not C.COMPANY_DIR.exists()


def test_today_shows_thaw_line(isolated):
    app = AppTest.from_string("from lib.ui_company import render_today\nrender_today(1.5)").run(timeout=30)
    app.button(key="today_sample").click().run(timeout=30)
    assert not app.exception
    assert any(m.value.startswith("**해동**") for m in app.markdown)
