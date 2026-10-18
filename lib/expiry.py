"""소비기한(사용기한) 손실: 계획대로 써도 기한까지 다 못 쓰고 남는 양(폐기 예상)과 임박 로트, 대응안.

계산
  - '원료 소요·재고'와 같은 60일 소요량(명시된 계획 + 계획 뒤 하루 평균)을 로트 단위로 다시 돌린다.
  - 로트의 기한(아래 '유효 기한')이 빠른 것부터 쓰고, 유효 기한일이 끝날 때 남은 양을 폐기 예상으로 본다. 그 뒤 소요는 다른 로트에서 쓴다.
  - 유효 기한 = 사용기한(입력)
                사용기한 미입력은 들어온 날(재고는 기준일)부터 가정 일수(기본 냉장 10일·냉동 365일)  -- '기한 가정' 표시, 경고에만 쓴다
                이미 해동 중인 냉동 로트(사용가능일 입력)는 해동 완료일 + 해동 후 보관 한도도 함께 본다(더 이른 날)
  - 검사대기·사용보류 로트는 쓰지 않으므로 기한 안에 상태가 바뀌지 않으면 전량 폐기 위험으로 따로 표시한다.
  - 수량은 매입(냉동) 무게로 보여 준다. 손실 = 폐기 예상 × (로트 단가, 없으면 원료 평균 단가 + 폐기 처리비).
  - 품질·안전 판정이나 법적 기한 결정이 아니다. 입력한 기한과 가정으로 계산한 경고다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from lib import company as C
from lib import cost as K
from lib import planning as P

EPS = 1e-6


@dataclass
class Expiry:
    lots: pd.DataFrame
    base_date: pd.Timestamp
    reference: pd.Timestamp
    horizon_end: pd.Timestamp

    @property
    def waste(self) -> pd.DataFrame:
        return self.lots[self.lots["폐기예상_kg"] > 0.5]

    @property
    def soon(self) -> pd.DataFrame:
        return self.lots[(self.lots["상태"] == "임박")]


def _effective(lot: dict, base: pd.Timestamp, st_: C.Settings) -> tuple[pd.Timestamp, str]:
    storage = lot["key"][3]
    if pd.notna(lot["expires"]):
        exp, why = pd.Timestamp(lot["expires"]), "입력"
    else:
        start = base if lot["kind"] == "inventory" else lot["arrival"]
        exp, why = start + pd.Timedelta(days=int(st_.shelf[storage])), f"가정({st_.shelf[storage]}일)"
    if storage == "냉동" and lot["explicit_ready"] and lot["thaw_days"]:
        thawed = pd.Timestamp(lot["ready"]) + pd.Timedelta(days=int(st_.thaw_hold))
        if thawed <= exp:  # material_lots가 이미 이 한도를 사용기한에 넣었다(근거만 표시)
            exp, why = thawed, f"해동 후 보관 한도({st_.thaw_hold}일)"
    return exp, why


def analyze(data: C.CompanyData, settings: C.Settings | None = None, today: pd.Timestamp | None = None) -> Expiry:
    if not data.ready():
        raise ValueError("입력 오류를 먼저 수정해 주세요.")
    st_ = settings or C.Settings()
    proj = C.project(data, today=today, settings=st_)
    base, ref = proj.base_date, proj.reference_date
    dates = pd.date_range(base + pd.Timedelta(days=1), periods=C.HORIZON_DAYS)
    need = {o: g.set_index("날짜")["소요량_kg"] for o, g in proj.daily.groupby("원료")}
    avg_price = {k: v for k, (v, _) in K.unit_price(K.material_prices(data), "avg").items()} if K.has_prices(data) else {}
    rows = []
    lots = [dict(l) for l in C.material_lots(data, st_)]
    for lot in lots:
        lot["exp"], lot["why"] = _effective(lot, base, st_)
        lot["start_qty"] = lot["qty"]
        lot["waste"] = None
    by_key: dict[tuple, list[dict]] = {}
    for lot in lots:
        by_key.setdefault(lot["key"], []).append(lot)
    for key, mine in by_key.items():
        label = C.ingredient_label(*key)
        dem = need.get(label, pd.Series(0.0, index=dates))
        for day in dates:
            want = float(dem.get(day, 0.0))
            for lot in sorted((l for l in mine if l["status"] == "사용가능" and l["ready"] <= day and day <= l["exp"] and l["qty"] > EPS),
                              key=lambda l: (l["exp"], l["ready"], l["id"])):
                if want <= EPS:
                    break
                used = min(lot["qty"], want)
                lot["qty"] -= used
                want -= used
            for lot in mine:
                if lot["waste"] is None and lot["exp"] <= day:
                    lot["waste"] = lot["qty"]  # 유효 기한일이 끝날 때 남은 양
    for lot in lots:
        storage = lot["key"][3]
        f = 1 / (1 - lot["loss"] / 100)          # 해동 후 무게 -> 매입(냉동) 무게
        start = lot["start_qty"] * f
        if lot["status"] != "사용가능":
            waste = start if lot["exp"] <= dates[-1] else 0.0
            state = f"{lot['status']} — 기한 전 판정 필요" if lot["exp"] <= dates[-1] else lot["status"]
        else:
            waste = (lot["waste"] or 0.0) * f if lot["waste"] is not None else 0.0
            left = (lot["exp"] - ref).days
            if lot["exp"] < dates[0] and lot["qty"] > EPS:
                state = "기한 지남"
            elif waste > 0.5:
                state = "폐기 예상"
            elif left <= st_.expiry_warn[storage]:
                state = "임박"
            elif lot["exp"] > dates[-1]:
                state = "계산 범위 밖"
            else:
                state = "기한 내 사용"
        label = C.ingredient_label(*lot["key"])
        price = lot["price"] if not math.isnan(lot["price"]) else avg_price.get(label, math.nan)
        rows.append({"원료": label, "로트": lot["id"], "구분": "재고" if lot["kind"] == "inventory" else "입고 예정", "보관": storage,
                     "수량_kg": start, "유효기한": lot["exp"], "기한근거": lot["why"], "남은일수": (lot["exp"] - ref).days,
                     "기한까지사용_kg": max(start - waste, 0.0) if lot["status"] == "사용가능" else 0.0, "폐기예상_kg": waste,
                     "단가_원kg": price, "손실_원": waste * (price + st_.disposal_cost) if not math.isnan(price) else math.nan,
                     "상태": state, "사용상태": lot["status"], "육종": lot["key"][0], "부위": lot["key"][1], "원산지": lot["key"][2]})
    order = {"기한 지남": 0, "폐기 예상": 1, "임박": 2}
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values(["상태", "유효기한"], key=lambda s: s.map(lambda v: order.get(v, 5)) if s.name == "상태" else s).reset_index(drop=True)
    return Expiry(df, base, ref, dates[-1])


# ------------------------------------------------------------------ 대응안
@dataclass
class Fix:
    name: str
    lot: str
    data: C.CompanyData
    notes: list[str]
    warnings: list[str]


def _last_day(data: C.CompanyData, product: str, until: pd.Timestamp) -> pd.Timestamp | None:
    """until 이전(포함)에서 그 제품을 만드는 마지막 계획일, 없으면 until 이전의 마지막 평일."""
    base = data.inventory["기준일"].max()
    days = data.plan.loc[(data.plan["제품"] == product) & (data.plan["날짜"] > base) & (data.plan["날짜"] <= until), "날짜"]
    if len(days):
        return days.max()
    d = until
    while d > base:
        if d.weekday() < 5:
            return d
        d -= pd.Timedelta(days=1)
    return None


def _increase_target(data: C.CompanyData, row: pd.Series, settings: C.Settings | None):
    """(제품 배합 행, 증산일, 폐기 예상을 다 쓰는 증산량 kg). 그 원료의 배합 비율이 가장 큰 제품 = 적게 늘려도 되는 제품."""
    key = (row["육종"], row["부위"], row["원산지"], row["보관"])
    rec = data.recipe[(data.recipe[C.MATERIAL_KEY] == pd.Series(key, index=C.MATERIAL_KEY)).all(axis=1)]
    if rec.empty:
        return None
    r = rec.sort_values("투입량_kg", ascending=False).iloc[0]
    thawed = float(row["폐기예상_kg"]) * (1 - K.thaw_loss(data, key, settings) / 100)
    extra = math.ceil(thawed / float(r["투입량_kg"]) / C.ROUND_KG) * C.ROUND_KG
    day = _last_day(data, r["제품"], pd.Timestamp(row["유효기한"]))
    if day is None or extra <= 0:
        return None
    return r, day, extra


def _with_extra(data: C.CompanyData, product: str, day: pd.Timestamp, extra: float) -> C.CompanyData:
    plan = data.plan.copy()
    template = plan[plan["제품"] == product].iloc[0].to_dict() if (plan["제품"] == product).any() else {"제품": product}
    template.update({"날짜": day, "계획물량_kg": float(extra), "주문번호": "폐기방지-증산", "납기": pd.NaT, "우선순위": 1})
    cand = P.change_scenario(data, plan=pd.concat([plan, pd.DataFrame([template])], ignore_index=True))
    cand.substitute, cand.supplier = data.substitute, data.supplier
    return cand


def _increase(data: C.CompanyData, row: pd.Series, r: pd.Series, day: pd.Timestamp, extra: float, name: str, extra_note: str = "") -> Fix:
    key = (row["육종"], row["부위"], row["원산지"], row["보관"])
    cand = _with_extra(data, r["제품"], day, extra)
    other = [x for x in data.recipe.loc[data.recipe["제품"] == r["제품"]].itertuples() if (x.육종, x.부위, x.원산지, x.보관) != key]
    notes = [f"{r['제품']}을 {day:%m/%d}에 {extra:,.0f}kg 더 만듭니다(제품 1kg당 {row['부위']} {float(r['투입량_kg']):.2f}kg)."]
    if other:
        notes.append("함께 더 쓰는 원료: " + ", ".join(f"{x.부위}({x.원산지}) {extra * x.투입량_kg:,.0f}kg" for x in other))
    if extra_note:
        notes.append(extra_note)
    return Fix(name, row["로트"], cand, notes, ["늘린 생산분의 판매·완제품 보관 계획과 설비·인력 여유가 필요합니다"])


def increase_fix(data: C.CompanyData, row: pd.Series, settings: C.Settings | None = None) -> Fix | None:
    """폐기 예상을 다 쓰는 만큼 증산한다. 다른 원료가 모자라면 부족이 생길 수 있다(비교표에 그대로 보인다)."""
    t = _increase_target(data, row, settings)
    if t is None:
        return None
    r, day, extra = t
    return _increase(data, row, r, day, extra, f"증산: {r['제품']} {day:%m/%d} +{extra:,.0f}kg")


def capped_increase_fix(data: C.CompanyData, row: pd.Series, settings: C.Settings | None = None) -> Fix | None:
    """다른 원료가 허용하는 만큼만 증산한다: 생산 부족이 늘지 않는 가장 큰 증산량(100kg 단위, 이분 탐색).

    다 쓰는 증산이 이미 부족을 늘리지 않으면 None(같은 안이라서). 한 kg도 늘릴 수 없으면 None.
    """
    t = _increase_target(data, row, settings)
    if t is None:
        return None
    r, day, extra = t
    end = max(data.plan["날짜"].max(), day)
    base_short = P.simulate(data, end, settings).metrics()["생산부족_kg"]

    def ok(q: float) -> bool:
        return P.simulate(_with_extra(data, r["제품"], day, q), end, settings).metrics()["생산부족_kg"] <= base_short + 0.5

    steps = int(extra // C.ROUND_KG)
    if ok(extra):
        return None
    lo, hi = 0, steps  # ok(lo*100) 참, ok(hi*100) 거짓
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid * C.ROUND_KG):
            lo = mid
        else:
            hi = mid
    if lo == 0:
        return None
    q = lo * C.ROUND_KG
    return _increase(data, row, r, day, q, f"증산(부족 없는 한도): {r['제품']} {day:%m/%d} +{q:,.0f}kg",
                     f"다른 원료를 끌어 써서 다른 계획을 못 만들게 되지 않는 한도입니다(폐기 예상을 다 쓰려면 {extra:,.0f}kg 필요).")


def divert_fix(data: C.CompanyData, row: pd.Series, settings: C.Settings | None = None) -> Fix | None:
    """대체표를 거꾸로: 이 원료가 '대체 원료'로 허용된 제품에서, 기한 전 계획의 원래 원료 일부를 이 원료로 바꾼다."""
    if data.substitute is None or not len(data.substitute):
        return None
    key = (row["육종"], row["부위"], row["원산지"], row["보관"])
    rules = data.substitute[[C.substitute_key(r) == key for _, r in data.substitute.iterrows()]]
    if rules.empty:
        return None
    from lib import response as R
    base = data.inventory["기준일"].max()
    until = pd.Timestamp(row["유효기한"])
    loss = K.thaw_loss(data, key, settings)
    thawed = float(row["폐기예상_kg"]) * (1 - loss / 100)
    best = None
    for _, rule in rules.iterrows():
        orig = (rule["육종"], rule["부위"], rule["원산지"], rule["보관"])
        products = [rule["제품"]] if rule["제품"] != "*" else sorted(set(data.recipe.loc[(data.recipe[C.MATERIAL_KEY] == pd.Series(orig, index=C.MATERIAL_KEY)).all(axis=1), "제품"]))
        for product in products:
            m = data.recipe[(data.recipe["제품"] == product) & (data.recipe[C.MATERIAL_KEY] == pd.Series(orig, index=C.MATERIAL_KEY)).all(axis=1)]
            rows = data.plan[(data.plan["제품"] == product) & (data.plan["날짜"] > base) & (data.plan["날짜"] <= until)]
            if m.empty or rows.empty:
                continue
            ratio = float(m["투입량_kg"].sum())
            capacity = float(rows["계획물량_kg"].sum()) * ratio * float(rule["환산계수"])
            rate = min(R._rate_step(thawed / capacity) if capacity > 0 else 1.0, float(rule["최대대체비율"]) / 100)
            if rate <= 0:
                continue
            variant = f"{product} (대체 {rate * 100:.0f}%)"
            new = []
            for _, x in data.recipe[data.recipe["제품"] == product].iterrows():
                if tuple(x[c] for c in C.MATERIAL_KEY) == orig:
                    if rate < 1:
                        new.append(dict(x, 제품=variant, 투입량_kg=float(x["투입량_kg"]) * (1 - rate)))
                    new.append({"제품": variant, "육종": key[0], "부위": key[1], "원산지": key[2], "보관": key[3],
                                "투입량_kg": float(x["투입량_kg"]) * rate * float(rule["환산계수"])})
                else:
                    new.append(dict(x, 제품=variant))
            recipe = pd.concat([data.recipe, pd.DataFrame(new)], ignore_index=True)
            plan = data.plan.copy()
            plan.loc[rows.index, "제품"] = variant
            cand = P.change_scenario(data, plan=plan, recipe=recipe)
            cand.substitute, cand.supplier = data.substitute, data.supplier
            used = float(rows["계획물량_kg"].sum()) * ratio * rate * float(rule["환산계수"])
            if best is None or used > best[0]:
                notes = [f"{product}의 {until:%m/%d}까지 계획 {len(rows)}건에서 {orig[1]}({orig[2]})의 {rate * 100:.0f}%를 {key[1]}({key[2]})로 바꿉니다(약 {used:,.0f}kg 사용)."]
                warns = R.origin_changes(data, cand)
                best = (used, Fix(f"돌리기: {variant}", row["로트"], cand, notes,
                                  (["원산지 표시 확인: " + "; ".join(warns)] if warns else []) + ["대체 배합 품질·고객 기준 확인이 필요합니다"]))
    return best[1] if best else None


def evaluate(data: C.CompanyData, fix: Fix, settings: C.Settings | None = None, today=None) -> dict:
    """대응 전후: 해당 로트 폐기 예상·전체 폐기 손실, 생산 부족 변화."""
    a, b = analyze(data, settings, today), analyze(fix.data, settings, today)
    lot_a = a.lots.loc[a.lots["로트"] == fix.lot, "폐기예상_kg"].sum()
    lot_b = b.lots.loc[b.lots["로트"] == fix.lot, "폐기예상_kg"].sum()
    end = max(data.plan["날짜"].max(), fix.data.plan["날짜"].max())
    sa, sb = P.simulate(data, end, settings).metrics(), P.simulate(fix.data, end, settings).metrics()
    return {"대응안": fix.name, "로트 폐기 예상 전": lot_a, "로트 폐기 예상 후": lot_b,
            "전체 폐기 손실 변화": (b.waste["손실_원"].sum() - a.waste["손실_원"].sum()) if a.lots["손실_원"].notna().all() else math.nan,
            "생산 부족 변화": sb["생산부족_kg"] - sa["생산부족_kg"], "계획량 변화": sb["계획량_kg"] - sa["계획량_kg"]}
