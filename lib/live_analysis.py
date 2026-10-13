"""실제(API) 데이터 분석: 재고 경보, 가격 경보, 예측+백테스트, 시차·상관 분석.

데이터 길이가 짧다(월 재고 31개, 일별 가격 수개월). 그래서 모든 예측은 단순 기준선과
롤링 백테스트로 비교하고, 가장 오차가 작은 모델을 쓰되 기준선보다 나은지 함께 보여준다.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import Ridge
from statsmodels.tsa.holtwinters import ExponentialSmoothing

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------- 재고 경보
def monthly_stock(stk: pd.DataFrame, kind: int, part: str = "합계") -> pd.Series:
    d = stk[(stk["kind"] == kind) & (stk["part"] == part)].sort_values("month")
    return d.set_index("month")["stock_kg"].asfreq("MS")


def stock_alerts(stk: pd.DataFrame, kind: int, k: float = 1.5) -> pd.DataFrame:
    """추세 + 연 1주기 계절(사인·코사인)을 최소제곱으로 맞추고 잔차 z-score로 경보한다.

    표본이 31개라 STL(12개월)은 과적합해 잔차가 0에 가까워지고 z가 의미 없는 직선이 됐다.
    그래서 모수 4개짜리 모델을 쓴다. 평가 대상 점도 적합에 포함되는 in-sample 방식이다.
    """
    s = monthly_stock(stk, kind).interpolate()
    n = len(s)
    t = np.arange(n)
    X = np.column_stack([np.ones(n), t, np.sin(2 * np.pi * t / 12), np.cos(2 * np.pi * t / 12)])
    y = s.to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    fit = X @ beta
    resid = y - fit
    sd = resid.std(ddof=X.shape[1])
    out = pd.DataFrame({"month": s.index, "stock_kg": y, "expected_kg": fit, "z": resid / sd})
    out["signal"] = np.select([out["z"] <= -k, out["z"] >= k], ["부족", "과잉"], "정상")
    return out


def part_alerts(stk: pd.DataFrame, kind: int, k: float = 1.5) -> pd.DataFrame:
    """부위별: 자기 이력 대비 최신 월의 z-score와 전월 변화."""
    rows = []
    d = stk[(stk["kind"] == kind) & (stk["part"] != "합계")]
    for part, g in d.groupby("part"):
        g = g.sort_values("month")
        if len(g) < 12:
            continue
        v = g["stock_kg"].to_numpy()
        z = (v[-1] - v.mean()) / v.std() if v.std() else 0.0
        rows.append({"part": part, "latest_kg": v[-1], "mom_pct": (v[-1] / v[-2] - 1) * 100, "z": z,
                     "signal": "부족" if z <= -k else "과잉" if z >= k else "정상"})
    return pd.DataFrame(rows).sort_values("z", key=abs, ascending=False)


# ---------------------------------------------------------------- 가격 경보
def price_alerts(pdl: pd.DataFrame, pm: pd.DataFrame, kinds: dict, gap_pct: float = 10.0) -> pd.DataFrame:
    """부위별: 평년 대비 %, 최근 4주 연속 상승/하락, 30일 변화율."""
    rows = []
    for (kind, item), g in pdl.groupby(["kind", "item"]):
        daily = g.groupby("date")["price_krw_kg"].mean().sort_index()
        if len(daily) < 10:
            continue
        weekly = daily.resample("W-MON").mean().dropna()
        diffs = weekly.diff().dropna().tail(4)
        streak = "4주 연속 상승" if len(diffs) == 4 and (diffs > 0).all() else "4주 연속 하락" if len(diffs) == 4 and (diffs < 0).all() else ""
        ago = daily[daily.index <= daily.index[-1] - pd.Timedelta(days=30)]
        chg30 = (daily.iloc[-1] / ago.iloc[-1] - 1) * 100 if len(ago) else np.nan
        m = pm[(pm["kind"] == kind) & (pm["item"] == item)]
        gap = np.nan
        if len(m):
            lm = m[m["month"] == m["month"].max()]
            gap = (lm["price_krw_kg"].mean() / lm["normal_krw_kg"].mean() - 1) * 100
        flags = []
        if abs(gap) >= gap_pct if not np.isnan(gap) else False:
            flags.append(f"평년 대비 {gap:+.0f}%")
        if streak:
            flags.append(streak)
        rows.append({"kind": kind, "종류": kinds.get(kind, kind), "item": item, "latest_krw_kg": daily.iloc[-1],
                     "gap_pct": gap, "chg30_pct": chg30, "streak": streak, "alert": ", ".join(flags)})
    return pd.DataFrame(rows).sort_values("gap_pct", key=lambda s: s.abs(), ascending=False)


# ---------------------------------------------------------------- 예측 + 롤링 백테스트
def _fourier(n_total: int, period: int, K: int = 2) -> np.ndarray:
    t = np.arange(n_total)
    cols = [t / n_total]
    for j in range(1, K + 1):
        cols += [np.sin(2 * np.pi * j * t / period), np.cos(2 * np.pi * j * t / period)]
    return np.column_stack(cols)


def _predict_models(train: np.ndarray, h: int, period: int | None, window: int) -> dict[str, np.ndarray]:
    out = {"직전값": np.repeat(train[-1], h), f"최근 {window}개 평균": np.repeat(train[-window:].mean(), h)}
    try:
        fit = ExponentialSmoothing(train, trend="add", damped_trend=True, initialization_method="estimated").fit()
        out["감쇠 추세(ETS)"] = fit.forecast(h)
    except Exception:
        pass
    if period and len(train) >= period + 4:
        out["전년 동기"] = np.array([train[len(train) - period + i % period] for i in range(h)])
        n = len(train)
        X = _fourier(n + h, period)
        mu, sd = train.mean(), train.std() or 1.0
        out["추세+계절(릿지)"] = Ridge(alpha=1.0).fit(X[:n], (train - mu) / sd).predict(X[n:]) * sd + mu
    return out


def backtest_models(s: pd.Series, h: int, period: int | None, window: int, n_origins: int, step: int = 1) -> pd.DataFrame:
    """롤링 예측: 여러 시점에서 h 구간을 예측해 모델별 평균 MAPE를 낸다."""
    v = s.to_numpy(dtype=float)
    errs: dict[str, list] = {}
    first = max(len(v) - h - (n_origins - 1) * step, (period or 0) + 6, 12)
    for t in range(first, len(v) - h + 1, step):
        preds = _predict_models(v[:t], h, period, window)
        for name, p in preds.items():
            errs.setdefault(name, []).extend(np.abs(p - v[t:t + h]) / np.abs(v[t:t + h]) * 100)
    rows = [{"모델": n, "MAPE(%)": float(np.mean(e)), "평가 횟수": len(e)} for n, e in errs.items() if e]
    return pd.DataFrame(rows).sort_values("MAPE(%)").reset_index(drop=True)


def forecast_series(s: pd.Series, h: int, period: int | None, window: int, n_origins: int, step: int = 1) -> dict:
    """백테스트에서 가장 좋은 모델로 미래 h개를 예측. 직전값 기준선 대비 개선율을 함께 반환."""
    bt = backtest_models(s, h, period, window, n_origins, step)
    best = bt.iloc[0]["모델"]
    v = s.to_numpy(dtype=float)
    preds = _predict_models(v, h, period, window)
    pred = preds[best]
    # 구간: 백테스트 평균 절대 퍼센트 오차를 대략적인 폭으로 사용 (표본이 작아 참고용)
    err = bt.iloc[0]["MAPE(%)"] / 100 * 1.25
    freq = s.index.freq or pd.infer_freq(s.index)
    idx = pd.date_range(s.index[-1], periods=h + 1, freq=freq)[1:]
    base_mape = float(bt.loc[bt["모델"] == "직전값", "MAPE(%)"].iloc[0]) if (bt["모델"] == "직전값").any() else np.nan
    return {"best": best, "backtest": bt, "index": idx, "pred": pred, "lo": pred * (1 - 1.96 * err), "hi": pred * (1 + 1.96 * err),
            "all": {k_: v_ for k_, v_ in preds.items()}, "improve_pct": (1 - bt.iloc[0]["MAPE(%)"] / base_mape) * 100 if base_mape else np.nan}


def daily_price_series(pdl: pd.DataFrame, kind: int, item: str) -> pd.Series:
    g = pdl[(pdl["kind"] == kind) & (pdl["item"] == item)].groupby("date")["price_krw_kg"].mean().sort_index()
    return g.asfreq("B").interpolate()  # 평일 달력, 공휴일 결측은 보간


# ---------------------------------------------------------------- 시차·상관
def lead_lag(a: pd.Series, p: pd.Series, max_lag: int = 6) -> pd.DataFrame:
    """a(도매·경락)의 주간 변화율이 p(소비자가)의 변화율을 L주 앞서는지. 표본 수와 p값을 함께 반환."""
    w = pd.concat([a.resample("W-MON").mean(), p.resample("W-MON").mean()], axis=1, keys=["a", "p"]).dropna()
    ca, cp = w["a"].pct_change().dropna(), w["p"].pct_change().dropna()
    rows = []
    for L in range(0, max_lag + 1):
        x, y = ca.iloc[:len(ca) - L] if L else ca, cp.iloc[L:]
        n = min(len(x), len(y))
        if n < 6:
            continue
        r, pv = stats.pearsonr(x.iloc[:n].to_numpy(), y.iloc[:n].to_numpy())
        rows.append({"lag": L, "corr": r, "p": pv, "n": n})
    return pd.DataFrame(rows)


def stock_price_corr(stock: pd.Series, price_m: pd.Series, max_lag: int = 3) -> pd.DataFrame:
    """월 재고 변화율 vs 이후 L개월 월평균 소비자가 변화율."""
    j = pd.concat([stock, price_m], axis=1, keys=["s", "p"]).dropna()
    cs, cp = j["s"].pct_change().dropna(), j["p"].pct_change().dropna()
    rows = []
    for L in range(0, max_lag + 1):
        x, y = (cs.iloc[:len(cs) - L] if L else cs), cp.iloc[L:]
        n = min(len(x), len(y))
        if n < 6:
            continue
        r, pv = stats.pearsonr(x.iloc[:n].to_numpy(), y.iloc[:n].to_numpy())
        rows.append({"lag_months": L, "corr": r, "p": pv, "n": n})
    return pd.DataFrame(rows)
