"""환율: API 응답 해석·오류 코드, 누적 수집(주말·휴일·한도), 변화율, USD 단가 환산, 원가 반영, 알림, 화면. 네트워크는 쓰지 않는다."""
import math

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from lib import alerts, live
from lib import company as C
from lib import cost as K
from lib import fx


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeSession:
    """날짜별 응답을 돌려준다. rates: {YYYYMMDD: USD 값} — 없는 날짜는 빈 목록(휴일)."""
    def __init__(self, rates, code=1):
        self.rates, self.code, self.calls = rates, code, []

    def get(self, url, params, timeout):
        self.calls.append(params["searchdate"])
        v = self.rates.get(params["searchdate"])
        if v is None:
            return FakeResp([])
        return FakeResp([{"result": self.code, "cur_unit": "USD", "deal_bas_r": f"{v:,.2f}"},
                         {"result": self.code, "cur_unit": "EUR", "deal_bas_r": "1,500.00"}])


@pytest.fixture
def fxdir(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setenv(fx.KEY_ENV, "test-key")
    return tmp_path


def _rates(start="2026-01-01", days=60, base=1300.0, step=1.0):
    d = pd.Timestamp(start)
    out = {}
    for i in range(days):
        day = d + pd.Timedelta(days=i)
        if day.weekday() < 5:
            out[f"{day:%Y%m%d}"] = base + step * i
    return out


def test_fetch_parses_comma_numbers(fxdir):
    df = fx.fetch(pd.Timestamp("2026-01-02"), FakeSession({"20260102": 1350.5}))
    assert df.set_index("cur_unit")["deal_bas_r"]["USD"] == 1350.5


def test_fetch_error_codes(fxdir):
    with pytest.raises(fx.FxError, match="인증"):
        fx.fetch(pd.Timestamp("2026-01-02"), FakeSession({"20260102": 1}, code=3))
    with pytest.raises(fx.FxError, match="한도"):
        fx.fetch(pd.Timestamp("2026-01-02"), FakeSession({"20260102": 1}, code=4))


def test_collect_skips_weekends_and_remembers_holidays(fxdir):
    rates = _rates(days=31)
    rates.pop("20260115")                          # 평일 휴일
    s = FakeSession(rates)
    r = fx.collect(30, today=pd.Timestamp("2026-01-31"), session=s)
    assert r["error"] == "" and all(pd.Timestamp(x).weekday() < 5 for x in s.calls)
    assert len(fx.load()["date"].unique()) == len(rates) and "2026-01-15" in fx._state()["empty"]
    s2 = FakeSession(rates)
    fx.collect(30, today=pd.Timestamp("2026-01-31"), session=s2)
    assert s2.calls == []                           # 받은 날짜·휴일은 다시 부르지 않음


def test_collect_budget_and_today_not_marked_empty(fxdir):
    s = FakeSession({})
    r = fx.collect(10, budget=3, today=pd.Timestamp("2026-01-09"), session=s)
    assert r["calls"] == 3 and r["pending"] > 0
    assert "2026-01-09" not in fx._state()["empty"]  # 오늘은 고시 전일 수 있다


def test_collect_stops_on_auth_error(fxdir):
    r = fx.collect(10, today=pd.Timestamp("2026-01-09"), session=FakeSession(_rates(days=10), code=3))
    assert "인증" in r["error"] and r["calls"] == 0


def test_change_and_latest(fxdir):
    fx.collect(60, today=pd.Timestamp("2026-03-01"), session=FakeSession(_rates(days=60, step=1.0)))
    day, rate = fx.latest()
    assert day == pd.Timestamp("2026-02-27") and rate == pytest.approx(1300 + 57)
    assert fx.change("USD", 30) == pytest.approx((1357 / (1300 + 27) - 1) * 100)


def test_usd_price_converted_with_latest_rate(fxdir):
    fx.collect(10, today=pd.Timestamp("2026-01-09"), session=FakeSession({"20260109": 1400.0}))
    raw = C.sample_tables()
    raw["inbound"]["단가_원kg"] = [None, None]
    raw["inbound"]["단가_USDkg"] = [4.0, 7.5]
    d = C.load_data(raw)
    assert d.ready()
    assert d.inbound["단가_원kg"].tolist() == [4.0 * 1400, 7.5 * 1400]
    assert any("USD 1,400.00원" in i.message for i in d.issues)


def test_usd_price_without_fx_data_warns(fxdir):
    raw = C.sample_tables()
    raw["inbound"]["단가_원kg"] = [None, None]
    raw["inbound"]["단가_USDkg"] = [4.0, 7.5]
    d = C.load_data(raw)
    assert d.inbound["단가_원kg"].isna().all() and any("환율 데이터가 없어" in i.message for i in d.issues)


def test_fx_changes_only_imports_with_pass_through(fxdir):
    fx.collect(60, today=pd.Timestamp("2026-03-01"), session=FakeSession(_rates(days=60, step=1.0)))
    d = C.load_data(C.sample_tables())
    s = C.Settings()
    s.fx_pass = 50.0
    chg = K.fx_changes(d, s)
    assert chg and all("수입" in k for k in chg)
    assert next(iter(chg.values()))[0] == pytest.approx(round(fx.change() * 0.5, 2))


def test_fx_alert(fxdir, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", fxdir / "company")
    fx.collect(60, today=pd.Timestamp("2026-03-01"), session=FakeSession(_rates(days=60, step=3.0)))  # 30일 약 +6.6%
    assert any(e["rule"] == "fx" and "USD" in e["message"] for e in alerts.find_alerts(alerts.CONFIG))


def test_fx_page(fxdir, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", fxdir / "company")
    fx.collect(60, today=pd.Timestamp("2026-03-01"), session=FakeSession(_rates(days=60)))
    app = AppTest.from_string("import streamlit as st\nst.session_state['co_source']='sample'\n"
                              "from lib.ui_fx import render_fx\nrender_fx(1.5, lambda f, *a, **k: f)").run(timeout=60)
    assert not app.exception
    assert any("수입 원료 단가" in m.value for m in app.markdown)


def test_fx_scenarios_scale_with_move_and_pass(fxdir):
    d = C.load_data(C.sample_tables())
    sc = K.fx_scenarios(d, C.Settings(), (5.0, 10.0)).set_index("USD 변화")
    assert sc.loc[10.0, "계획 기간 원료비 변화"] == pytest.approx(2 * sc.loc[5.0, "계획 기간 원료비 변화"], rel=1e-6)
    assert sc.loc[10.0, "원료비율 후"] > sc.loc[10.0, "원료비율 전"]
    half = C.Settings()
    half.fx_pass = 50.0
    assert K.fx_scenarios(d, half, (10.0,)).iloc[0]["계획 기간 원료비 변화"] == pytest.approx(sc.loc[5.0, "계획 기간 원료비 변화"], rel=1e-6)
