"""실제 데이터 분석 함수: 경보·백테스트·시차."""
import numpy as np
import pandas as pd

from lib import live_analysis as LA


def _stock_frame(values, kind=4304):
    months = pd.date_range("2024-01-01", periods=len(values), freq="MS")
    return pd.DataFrame({"month": months, "kind": kind, "part": "합계", "stock_kg": values})


def test_stock_alert_flags_injected_spike_and_not_flat():
    rng = np.random.default_rng(0)
    base = 1e7 + rng.normal(0, 1e5, 30)
    spiked = base.copy()
    spiked[-1] += 1.5e6
    assert LA.stock_alerts(_stock_frame(spiked), 4304, 2.0).iloc[-1]["signal"] == "과잉"
    assert (LA.stock_alerts(_stock_frame(base), 4304, 3.0)["signal"] == "정상").all()


def test_stock_alert_z_not_degenerate():
    """STL 과적합 회귀: 서로 다른 두 시계열의 z가 부호만 다른 직선이 되면 안 된다."""
    rng = np.random.default_rng(1)
    a = LA.stock_alerts(_stock_frame(1e7 + rng.normal(0, 1e5, 31)), 4304)
    b = LA.stock_alerts(_stock_frame(5e6 + rng.normal(0, 1e5, 31), 4301), 4301)
    assert abs(np.corrcoef(a["z"], b["z"])[0, 1]) < 0.9
    assert a["z"].std() > 0.5


def test_backtest_includes_naive_baseline_and_sorted():
    rng = np.random.default_rng(2)
    s = pd.Series(np.linspace(100, 130, 60) + rng.normal(0, 1, 60), index=pd.date_range("2026-01-01", periods=60, freq="B"))
    bt = LA.backtest_models(s, 5, None, 7, 6, step=3)
    assert "직전값" in set(bt["모델"])
    assert bt["MAPE(%)"].is_monotonic_increasing


def test_forecast_picks_best_and_reports_improvement():
    s = pd.Series(np.linspace(100, 200, 60), index=pd.date_range("2026-01-01", periods=60, freq="B"))
    f = LA.forecast_series(s, 5, None, 7, 6, step=3)
    assert len(f["pred"]) == 5 and (f["lo"] <= f["pred"]).all() and (f["pred"] <= f["hi"]).all()
    assert f["best"] != "직전값"  # 추세가 뚜렷하면 추세 모델이 직전값을 이겨야 함


def test_price_alert_streak_and_gap():
    days = pd.bdate_range("2026-08-03", periods=40)
    pdl = pd.DataFrame({"date": days, "kind": 4304, "item": "삼겹살", "grade": "-", "price_krw_kg": np.linspace(1000, 1400, 40)})
    pm = pd.DataFrame({"month": [pd.Timestamp("2026-09-01")], "kind": 4304, "item": "삼겹살", "grade": "-",
                       "price_krw_kg": [1300.0], "normal_krw_kg": [1000.0]})
    r = LA.price_alerts(pdl, pm, {4304: "돼지"}, 10.0).iloc[0]
    assert r["streak"] == "4주 연속 상승"
    assert round(r["gap_pct"]) == 30 and "평년" in r["alert"]


def test_lead_lag_detects_known_lag():
    idx = pd.date_range("2026-01-05", periods=60, freq="W-MON")
    rng = np.random.default_rng(3)
    a = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.03, 60)), index=idx)
    p = a.shift(2).bfill()  # 소비자가는 경락가를 2주 뒤따라간다
    ll = LA.lead_lag(a, p)
    assert ll.loc[ll["corr"].idxmax(), "lag"] == 2
