"""원가 영향: 로트 단가 × 배합표로 제품 kg당 원료비를 계산하고, 단가 변동 가정을 적용한다.

- 단가는 재고·입고예정 CSV의 '단가_원kg'(선택 열). 단가가 없는 원료를 0원으로 계산하지 않는다.
- 판매가는 생산계획의 '판매가_원kg'(선택 열). 있으면 원료비율을 보여 준다.
- 시장 소비자가는 매입가가 아니다. 변화율을 '가정'으로 옮겨 쓸 수 있을 뿐 실제 매입가를 예측하지 않는다.
- 가공비·포장재·인건비·물류비는 포함하지 않는다(원료비만).
"""
from __future__ import annotations

import math

import pandas as pd

from lib import advice as A
from lib import company as C

PRICE = "단가_원kg"
SALE = "판매가_원kg"
BASIS = {"avg": "보유·입고 로트 평균 (장부 기준)", "next": "입고 예정 단가 (재구매 기준)"}


def has_prices(data: C.CompanyData) -> bool:
    return any(df is not None and PRICE in df and df[PRICE].notna().any() for df in (data.inventory, data.inbound))


def has_sales(data: C.CompanyData) -> bool:
    return data.plan is not None and SALE in data.plan and data.plan[SALE].notna().any()


def _lots(data: C.CompanyData) -> pd.DataFrame:
    """단가 계산에 쓰는 로트: 기준일 재고 + 기준일 이후 입고 예정."""
    base = data.inventory["기준일"].max()
    parts = []
    inv = data.inventory.assign(구분="재고", 날짜=base, 수량=data.inventory["재고량_kg"])
    parts.append(inv)
    if data.inbound is not None and len(data.inbound):
        inb = data.inbound[data.inbound["입고예정일"] > base]
        parts.append(inb.assign(구분="입고", 날짜=inb["입고예정일"], 수량=inb["입고량_kg"]))
    lots = pd.concat(parts, ignore_index=True)
    if PRICE not in lots:
        lots[PRICE] = float("nan")
    return lots[C.MATERIAL_KEY + ["구분", "날짜", "수량", PRICE]]


def material_prices(data: C.CompanyData) -> pd.DataFrame:
    """원료별 단가 요약. 단가가 하나도 없으면 단가는 NaN(0으로 채우지 않음)."""
    lots = _lots(data)
    keys = set(lots[C.MATERIAL_KEY].itertuples(index=False, name=None)) | set(data.recipe[C.MATERIAL_KEY].itertuples(index=False, name=None))
    rows = []
    for key in sorted(keys):
        g = lots[(lots[C.MATERIAL_KEY] == pd.Series(key, index=C.MATERIAL_KEY)).all(axis=1)]
        priced = g[g[PRICE].notna() & (g["수량"] > 0)]
        total = float(g["수량"].sum())
        avg = float((priced[PRICE] * priced["수량"]).sum() / priced["수량"].sum()) if len(priced) else math.nan
        nxt_rows = priced[priced["구분"] == "입고"].sort_values("날짜")
        nxt = float(nxt_rows[PRICE].iloc[-1]) if len(nxt_rows) else math.nan
        rows.append({"원료": C.ingredient_label(*key), "육종": key[0], "부위": key[1], "원산지": key[2], "보관": key[3],
                     "평균단가": avg, "입고예정단가": nxt,
                     "단가입력비율": float(priced["수량"].sum() / total) if total > 0 else (1.0 if len(priced) else 0.0)})
    return pd.DataFrame(rows)


def unit_price(prices: pd.DataFrame, basis: str) -> dict[str, tuple[float, str]]:
    """원료 -> (단가, 근거). 재구매 기준인데 입고 예정 단가가 없으면 평균으로 대신하고 근거에 남긴다."""
    out = {}
    for _, r in prices.iterrows():
        if basis == "next" and not math.isnan(r["입고예정단가"]):
            out[r["원료"]] = (r["입고예정단가"], "입고 예정 단가")
        elif not math.isnan(r["평균단가"]):
            out[r["원료"]] = (r["평균단가"], "로트 평균" + (" (입고 예정 단가 없음)" if basis == "next" else ""))
        else:
            out[r["원료"]] = (math.nan, "단가 없음")
    return out


def market_changes(data: C.CompanyData, market: A.Market) -> dict[str, tuple[float, str]]:
    """원료 -> (소비자가 한 달 변화율 %, 시장 품목). 연결되는 시세가 없는 원료는 빠진다."""
    out = {}
    for sp, part, origin, storage in data.recipe[C.MATERIAL_KEY].drop_duplicates().itertuples(index=False, name=None):
        info = A._market_for(sp, part, origin, market)["price"]
        if info and info["chg30"] is not None:
            out[C.ingredient_label(sp, part, origin, storage)] = (round(info["chg30"], 1), "소비자가 " + info["item"])
    return out


def _chg30(series: pd.Series) -> float:
    """최근 5거래일 평균 vs 30일 전 무렵 5거래일 평균 (%). 경락가는 하루 변동이 커서 평균으로 비교한다."""
    s = series.dropna().sort_index()
    if len(s) < 10:
        return math.nan
    ago = s[s.index <= s.index[-1] - pd.Timedelta(days=30)]
    if len(ago) < 3:
        return math.nan
    return (float(s.tail(5).mean()) / float(ago.tail(5).mean()) - 1) * 100


def auction_changes(data: C.CompanyData, beef: pd.DataFrame | None = None, pig: pd.DataFrame | None = None) -> dict[str, tuple[float, str]]:
    """원료 -> (경락가 30일 변화율 %, 근거). 국산만 연결한다(수입육 경락가는 없음).

    소: 부분육 경락가(한우, 전 도매시장 평균)를 같은 부위에 연결.
    돼지: 제주 돼지 도체 평균 경락가 하나를 모든 국산 돼지 부위에 공통으로 쓴다(부위별 경락가가 없음).
    """
    from lib import live
    try:
        beef = live.load("auction_beef") if beef is None else beef
        pig = live.load("auction_pig") if pig is None else pig
    except Exception:
        return {}
    beef_chg, beef_day, pig_chg, pig_day = {}, "", math.nan, ""
    if beef is not None and len(beef):
        beef_day = f"{pd.to_datetime(beef['date']).max():%m/%d}"
        for cut, g in beef.groupby("cut"):
            v = _chg30(g.set_index(pd.to_datetime(g["date"]))["han_krw_kg"])
            if not math.isnan(v):
                beef_chg[cut] = v
    if pig is not None and len(pig):
        g = pig[pig["grade"] == "평균"]
        pig_chg = _chg30(g.set_index(pd.to_datetime(g["date"]))["price_krw_kg"])
        pig_day = f"{pd.to_datetime(g['date']).max():%m/%d}" if len(g) else ""
    out = {}
    for sp, part, origin, storage in data.recipe[C.MATERIAL_KEY].drop_duplicates().itertuples(index=False, name=None):
        if origin != "국산":
            continue
        label = C.ingredient_label(sp, part, origin, storage)
        if sp == "소" and part in beef_chg:
            out[label] = (round(beef_chg[part], 1), f"한우 {part} 경락가({beef_day}까지)")
        elif sp == "돼지" and not math.isnan(pig_chg):
            out[label] = (round(pig_chg, 1), f"제주 돼지 도체 평균 경락가({pig_day}까지)")
    return out


SOURCES = {"auction": "경락가 (도매시장, 국산만)", "consumer": "소비자가 (소매)"}


def source_changes(data: C.CompanyData, market: A.Market, source: str) -> dict[str, tuple[float, str]]:
    if source == "auction":
        return auction_changes(data)
    return market_changes(data, market) if market.available else {}


def _plan_future(data: C.CompanyData) -> pd.DataFrame:
    return data.plan[data.plan["날짜"] > data.inventory["기준일"].max()]


def thaw_loss(data: C.CompanyData, key: tuple, settings: C.Settings | None = None) -> float:
    """원료의 해동 감량률(%). 냉장은 0. 재고 CSV에 값이 있으면 그 값, 없으면 기본값."""
    inv = data.inventory
    rows = inv[(inv[C.MATERIAL_KEY] == pd.Series(key, index=C.MATERIAL_KEY)).all(axis=1)]
    return C.lot_thaw(rows.iloc[0] if len(rows) else {}, key[2], key[3], settings or C.Settings())[1]


def components(data: C.CompanyData, basis: str = "avg", change_pct: dict[str, float] | None = None,
               settings: C.Settings | None = None) -> pd.DataFrame:
    """제품 x 원료 원료비 구성 (원/제품kg). 냉동 원료는 해동 감량만큼 더 사야 하므로 단가 ÷ (1 − 감량률)로 본다."""
    price = unit_price(material_prices(data), basis)
    pct = change_pct or {}
    rows = []
    for (product, *key), ratio in data.recipe.groupby(["제품"] + C.MATERIAL_KEY)["투입량_kg"].sum().items():
        label = C.ingredient_label(*key)
        p, src = price.get(label, (math.nan, "단가 없음"))
        loss = thaw_loss(data, tuple(key), settings)
        eff = p / (1 - loss / 100)
        eff2 = eff * (1 + pct.get(label, 0.0) / 100)
        rows.append({"제품": product, "원료": label, "투입량_kg": ratio, "단가": p, "해동감량률": loss,
                     "해동후단가": eff, "가정단가": eff2, "근거": src, "원료비": ratio * eff, "가정원료비": ratio * eff2})
    return pd.DataFrame(rows)


def product_costs(data: C.CompanyData, basis: str = "avg", change_pct: dict[str, float] | None = None,
                  settings: C.Settings | None = None) -> pd.DataFrame:
    """제품별 kg당 원료비 (현재·가정), 판매가 대비 원료비율, 명시된 계획 기간의 원료비 합계."""
    comp = components(data, basis, change_pct, settings)
    plan = _plan_future(data)
    rows = []
    for product, g in comp.groupby("제품", sort=False):
        missing = g.loc[g["단가"].isna(), "원료"].tolist()
        cur = math.nan if missing else float(g["원료비"].sum())
        new = math.nan if missing else float(g["가정원료비"].sum())
        p = plan[plan["제품"] == product]
        qty = float(p["계획물량_kg"].sum())
        sale = math.nan
        if SALE in p and p[SALE].notna().any():
            s = p[p[SALE].notna()]
            sale = float((s[SALE] * s["계획물량_kg"]).sum() / s["계획물량_kg"].sum()) if s["계획물량_kg"].sum() > 0 else float(s[SALE].mean())
        rows.append({"제품": product, "원료비_원kg": cur, "가정원료비_원kg": new, "변화_원kg": new - cur,
                     "변화율": (new / cur - 1) * 100 if cur and not math.isnan(cur) else math.nan,
                     "판매가_원kg": sale, "원료비율": cur / sale * 100 if sale == sale else math.nan,
                     "가정원료비율": new / sale * 100 if sale == sale else math.nan,
                     "계획물량_kg": qty, "계획원료비": cur * qty, "가정계획원료비": new * qty,
                     "단가없는원료": ", ".join(missing)})
    return pd.DataFrame(rows)


def material_amounts(data: C.CompanyData, proj: C.Projection, basis: str = "next",
                     change_pct: dict[str, float] | None = None, days: int = 14, settings: C.Settings | None = None) -> pd.DataFrame:
    """원료별 앞으로 N일 소요 금액과 발주 검토량 금액 (원). 재구매 기준 단가를 기본으로 쓴다.

    소요량은 해동 후 무게라 냉동 원료는 ÷ (1 − 감량률)로 매입 무게로 바꿔 금액을 낸다. 발주량은 이미 냉동(매입) 무게다.
    """
    price = unit_price(material_prices(data), basis)
    pct = change_pct or {}
    first = proj.daily[proj.daily["날짜"] <= proj.base_date + pd.Timedelta(days=days)]
    need = first.groupby("원료")["소요량_kg"].sum()
    rows = []
    for _, r in proj.summary.iterrows():
        p, src = price.get(r["원료"], (math.nan, "단가 없음"))
        p2 = p * (1 + pct.get(r["원료"], 0.0) / 100)
        order = float(r["발주량_kg"]) if proj.actionable else 0.0
        loss = thaw_loss(data, (r["육종"], r["부위"], r["원산지"], r["보관"]), settings)
        buy = float(need.get(r["원료"], 0.0)) / (1 - loss / 100)
        rows.append({"원료": r["원료"], "적용단가": p2, "근거": src, f"{days}일소요_kg": float(need.get(r["원료"], 0.0)),
                     f"{days}일소요금액": buy * p2,
                     "발주검토량_kg": order, "발주금액": order * p2 if order > 0 else 0.0})
    return pd.DataFrame(rows)
