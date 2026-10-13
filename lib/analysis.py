"""분석 로직: 경보, 예측, 계절성/이상치, 이벤트 효과, 수입 위험, 가격 시차, 유통 병목."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from statsmodels.tsa.seasonal import STL

# ---------------------------------------------------------------- 경보

def stock_alert_table(sup: pd.DataFrame, species: str, k: float, baseline: str) -> pd.DataFrame:
    """주별 재고 z-score와 신호(부족/정상/과잉). baseline: 'all' | 'rolling52'."""
    d = sup[sup["species"] == species].sort_values("week_start").copy()
    act = d["data_type"] == "actual"
    if baseline == "rolling52":
        base = d["ending_stock_kg"].where(act)
        mu = base.rolling(52, min_periods=26).mean().shift(1)
        sd = base.rolling(52, min_periods=26).std().shift(1)
        # 예측 구간은 마지막 실적 시점의 기준을 고정해서 사용
        mu = mu.where(act).ffill()
        sd = sd.where(act).ffill()
    else:
        mu = d.loc[act, "ending_stock_kg"].mean()
        sd = d.loc[act, "ending_stock_kg"].std()
    d["z"] = (d["ending_stock_kg"] - mu) / sd
    d["lower"] = mu - k * sd
    d["upper"] = mu + k * sd
    d["signal"] = np.select([d["z"] <= -k, d["z"] >= k], ["부족", "과잉"], "정상")
    return d


def signal_streak(signals: pd.Series) -> int:
    """마지막 신호가 연속된 횟수."""
    last, n = signals.iloc[-1], 0
    for s in signals.iloc[::-1]:
        if s != last:
            break
        n += 1
    return n


# ---------------------------------------------------------------- 예측

def _features(cal: pd.DataFrame, weeks: pd.Series, t0: pd.Timestamp) -> pd.DataFrame:
    c = cal.set_index("week_start").reindex(weeks)
    t = ((weeks - t0).dt.days / 7).to_numpy()
    f = pd.DataFrame(index=range(len(weeks)))
    f["trend"] = t / 52
    doy = weeks.dt.dayofyear.to_numpy() / 365.25
    for k in (1, 2, 3):
        f[f"s{k}"] = np.sin(2 * np.pi * k * doy)
        f[f"c{k}"] = np.cos(2 * np.pi * k * doy)
    w = c["weeks_to_holiday"].to_numpy()
    for off in (-1, 0, 1, 2, 3):
        f[f"hol{off}"] = (w == off).astype(float)
    f["samgyeopsal"] = c["is_samgyeopsal_day_week"].to_numpy().astype(float)
    f["summer"] = c["is_summer_vacation_season"].to_numpy().astype(float)
    f["family"] = c["is_family_month"].to_numpy().astype(float)
    return f


def fit_predict(sup: pd.DataFrame, cal: pd.DataFrame, species: str, col: str, horizon_weeks: pd.Series,
                train_end: pd.Timestamp | None = None, alpha: float = 1.0):
    """Ridge(추세+연간 푸리에+명절/이벤트 더미). 예측값과 잔차 표준편차를 반환한다."""
    d = sup[(sup["species"] == species) & (sup["data_type"] == "actual")].sort_values("week_start")
    if train_end is not None:
        d = d[d["week_start"] <= train_end]
    t0 = d["week_start"].min()
    X = _features(cal, d["week_start"].reset_index(drop=True), t0)
    y = d[col].to_numpy(dtype=float)
    mu, sd = y.mean(), y.std()
    model = Ridge(alpha=alpha).fit(X, (y - mu) / sd)
    resid = (y - mu) / sd - model.predict(X)
    Xh = _features(cal, horizon_weeks.reset_index(drop=True), t0)
    pred = model.predict(Xh) * sd + mu
    return pred, resid.std() * sd


def backtest(sup: pd.DataFrame, cal: pd.DataFrame, species: str, col: str, holdout: int = 13) -> dict:
    """마지막 holdout주를 숨기고 예측 -> MAPE. 계절 단순 예측(작년 같은 주)과 비교."""
    d = sup[(sup["species"] == species) & (sup["data_type"] == "actual")].sort_values("week_start")
    test = d.tail(holdout)
    train_end = d["week_start"].iloc[-holdout - 1]
    pred, _ = fit_predict(sup, cal, species, col, test["week_start"], train_end=train_end)
    actual = test[col].to_numpy(dtype=float)
    mape = float(np.mean(np.abs(pred - actual) / actual) * 100)
    # 계절 단순 예측 (52주 전)
    prev = d.set_index("week_start")[col]
    naive = np.array([prev.get(w - pd.Timedelta(weeks=52), np.nan) for w in test["week_start"]], dtype=float)
    mape_n = float(np.nanmean(np.abs(naive - actual) / actual) * 100)
    return {"mape_model": mape, "mape_naive": mape_n, "weeks": test["week_start"], "pred": pred, "actual": actual}


def forecast_frame(sup: pd.DataFrame, cal: pd.DataFrame, species: str, col: str) -> pd.DataFrame:
    """파일 forecast 구간에 대한 자체 모델 예측과 신뢰구간(95%)."""
    fc = sup[(sup["species"] == species) & (sup["data_type"] == "forecast")].sort_values("week_start")
    pred, sd = fit_predict(sup, cal, species, col, fc["week_start"])
    out = fc[["week_start", col]].rename(columns={col: "file_forecast"}).reset_index(drop=True)
    out["model"] = pred
    out["lo"] = pred - 1.96 * sd
    out["hi"] = pred + 1.96 * sd
    out["gap_pct"] = (out["file_forecast"] - out["model"]) / out["model"] * 100
    return out


# ---------------------------------------------------------------- 계절성 / 이상치

def stl_decompose(sup: pd.DataFrame, species: str, col: str) -> pd.DataFrame:
    d = sup[(sup["species"] == species) & (sup["data_type"] == "actual")].sort_values("week_start")
    s = d.set_index("week_start")[col].asfreq("W-MON").interpolate()
    res = STL(s, period=52, robust=True).fit()
    out = pd.DataFrame({"value": s, "trend": res.trend, "seasonal": res.seasonal, "resid": res.resid})
    mad = np.median(np.abs(out["resid"] - out["resid"].median())) * 1.4826
    out["resid_z"] = (out["resid"] - out["resid"].median()) / mad
    return out.reset_index()


# ---------------------------------------------------------------- 이벤트 효과

def weekly_consumption(meat: pd.DataFrame, species: str, cut_id: str | None = None) -> pd.DataFrame:
    d = meat[(meat["species"] == species) & (meat["data_type"] == "actual")]
    if cut_id:
        d = d[d["cut_id"] == cut_id]
    return d.groupby("week_start", as_index=False)["consumption_kg"].sum()


def event_index(cons: pd.DataFrame, cal: pd.DataFrame, window: int = 13) -> pd.DataFrame:
    """소비량 / 중심 이동 중앙값(window주) = 평소 대비 지수. 100이 평소 수준."""
    d = cons.sort_values("week_start").copy()
    base = d["consumption_kg"].rolling(window, center=True, min_periods=window // 2).median()
    d["index"] = d["consumption_kg"] / base * 100
    return d.merge(cal[["week_start", "holiday_name", "weeks_to_holiday", "is_samgyeopsal_day_week",
                        "is_summer_vacation_season", "is_family_month"]], on="week_start", how="left")


def holiday_curve(ev: pd.DataFrame) -> pd.DataFrame:
    d = ev[ev["weeks_to_holiday"].between(-2, 3)]
    g = d.groupby("weeks_to_holiday")["index"].agg(["mean", "count"]).reset_index()
    return g


# ---------------------------------------------------------------- 수입 위험

def import_risk(imp: pd.DataFrame, species: str) -> pd.DataFrame:
    d = imp[(imp["species"] == species)]
    g = d.groupby("month")
    out = pd.DataFrame({
        "hhi": g["import_share_pct"].apply(lambda s: float(((s / 100) ** 2).sum() * 10000)),
        "top_share": g["import_share_pct"].max(),
        "top_country": g.apply(lambda x: x.loc[x["import_share_pct"].idxmax(), "country"], include_groups=False),
        "cif_avg": g.apply(lambda x: np.average(x["cif_price_usd_kg"], weights=x["import_volume_kg"]),
                           include_groups=False),
        "weekly_avg_kg": g["weekly_avg_kg"].sum(),
        "data_type": g["data_type"].first(),
    }).reset_index()
    out["cif_yoy_pct"] = out["cif_avg"].pct_change(12) * 100
    return out


# ---------------------------------------------------------------- 가격 / 소비

def price_series(meat: pd.DataFrame, species: str, cut_id: str | None = None) -> pd.DataFrame:
    """소비량 가중 평균 도·소매가와 스프레드 (실적만)."""
    d = meat[(meat["species"] == species) & (meat["data_type"] == "actual")]
    if cut_id:
        d = d[d["cut_id"] == cut_id]
    d = d.dropna(subset=["wholesale_price_krw_kg", "retail_price_krw_kg"])
    w = d["consumption_kg"]
    g = d.assign(ws=d["wholesale_price_krw_kg"] * w, rs=d["retail_price_krw_kg"] * w, w=w).groupby("week_start")
    out = pd.DataFrame({"wholesale": g["ws"].sum() / g["w"].sum(), "retail": g["rs"].sum() / g["w"].sum(),
                        "consumption_kg": g["w"].sum()}).reset_index()
    out["spread"] = out["retail"] - out["wholesale"]
    return out


def lag_correlation(ps: pd.DataFrame, max_lag: int = 6) -> pd.DataFrame:
    """도매가 주간 변화율(t) vs 소매가 변화율(t+lag) 상관."""
    w = ps["wholesale"].pct_change()
    r = ps["retail"].pct_change()
    rows = [{"lag": L, "corr": w.corr(r.shift(-L))} for L in range(0, max_lag + 1)]
    return pd.DataFrame(rows)


def price_elasticity(ps: pd.DataFrame) -> float:
    """log-log 회귀 기울기 (소매가 -> 소비량). 단순 상관 기반 참고치."""
    d = ps.dropna()
    return float(np.polyfit(np.log(d["retail"]), np.log(d["consumption_kg"]), 1)[0])


# ---------------------------------------------------------------- 유통 병목

NODE_FLOWS = {  # 노드: (유입 flow, 유출 flow들)
    "공판장·도매시장": (["F1"], ["F3", "F4"]),
    "대형유통 물류센터": (["F2"], ["F5", "F6"]),
}


def flow_bottleneck(flow: pd.DataFrame, species: str | None) -> pd.DataFrame:
    """flow별 월 리드타임/마진 z-score(자체 이력 기준). 점수 = 리드타임 z + 마진 z 중 큰 쪽."""
    d = flow[flow["data_type"] == "actual"] if species is None else flow[(flow["species"] == species) & (flow["data_type"] == "actual")]
    g = d.groupby(["month", "flow_id", "from_stage", "to_stage"], as_index=False).agg(
        volume=("weekly_avg_kg", "sum"), lead=("lead_time_days", "mean"), margin=("margin_krw_kg", "mean"))
    for c in ("lead", "margin"):
        g[c + "_z"] = g.groupby("flow_id")[c].transform(lambda s: (s - s.mean()) / s.std())
    g["score"] = g[["lead_z", "margin_z"]].max(axis=1)
    return g


def node_balance(flow: pd.DataFrame, species: str | None) -> pd.DataFrame:
    """노드별 유출/유입 비율. 1보다 많이 낮으면 물량이 노드에 쌓이는 중."""
    d = flow[flow["data_type"] == "actual"] if species is None else flow[(flow["species"] == species) & (flow["data_type"] == "actual")]
    rows = []
    for node, (ins, outs) in NODE_FLOWS.items():
        p = d.pivot_table(index="month", columns="flow_id", values="weekly_avg_kg", aggfunc="sum")
        r = pd.DataFrame({"month": p.index, "node": node, "inflow": p[ins].sum(axis=1).to_numpy(),
                          "outflow": p[outs].sum(axis=1).to_numpy()})
        r["ratio"] = r["outflow"] / r["inflow"]
        rows.append(r)
    return pd.concat(rows, ignore_index=True)
