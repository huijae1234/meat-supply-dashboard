"""생산 차질 대응 후보: 대체 원료 / 긴급 현물 / 생산일 늦추기 / 감산 수용을 자동으로 만들고 비용·부족·납기를 비교한다.

- 후보는 원본 CSV의 복사본이다. 실제 발주·계획 변경을 하지 않는다.
- 비용(원)은 '감산 수용' 대비가 아니라 각 후보의 절대값이다.
    남은 감산 손실 = 부족량 × (판매가 − kg당 원료비)   (공헌이익 손실: 안 쓴 원료비는 빼고 본다)
    위약금        = 위약금_원kg × (부족량, 또는 납기보다 늦게 계획된 행은 계획 물량)   (생산계획에 열이 있을 때만)
    추가 비용     = 긴급 현물 할증분(수량 × 단가 × 할증%) + 대체로 바뀐 원료비 차이
- 판매가·단가가 없으면 금액은 '-'로 두고 kg·건수만 비교한다.
- 최적화가 아니라 규칙으로 만든 후보다. 고른 후보는 편집기에서 다듬는다.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field

import pandas as pd

from lib import capacity as Q
from lib import company as C
from lib import cost as K
from lib import planning as P

KINDS = ("감산 수용", "대체 원료", "긴급 현물 구매", "생산일 늦추기", "생산 능력 맞춰 나누기")


@dataclass
class Candidate:
    name: str
    data: C.CompanyData
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _label_keys(data: C.CompanyData) -> dict[str, tuple]:
    keys = set(data.recipe[C.MATERIAL_KEY].itertuples(index=False, name=None)) | set(data.inventory[C.MATERIAL_KEY].itertuples(index=False, name=None))
    return {C.ingredient_label(*k): k for k in keys}


def money(data: C.CompanyData, feas: P.Feasibility, settings: C.Settings | None = None) -> dict:
    """감산 손실(공헌이익)과 위약금. 판매가·원료비를 모르는 제품이 있으면 그 부분은 NaN."""
    if not K.has_sales(data):
        return {"감산손실": math.nan, "위약금": math.nan}
    unit = K.product_costs(data, "avg", None, settings).set_index("제품")["원료비_원kg"] if K.has_prices(data) else pd.Series(dtype=float)
    plan = data.plan
    loss, pen, unknown = 0.0, 0.0, False
    for _, o in feas.orders.iterrows():
        row = plan.loc[o["계획행"]]
        short = float(o["부족량_kg"])
        late = pd.notna(o["납기"]) and o["날짜"] > o["납기"]
        if short > 1e-6:
            sale = row.get(K.SALE)
            c = unit.get(o["제품"], math.nan)
            if pd.isna(sale) or pd.isna(c):
                unknown = True
            else:
                loss += short * max(float(sale) - float(c), 0.0)
        p = row.get("위약금_원kg")
        if pd.notna(p) if p is not None else False:
            pen += float(p) * (float(o["계획물량_kg"]) if late else short)
    return {"감산손실": math.nan if unknown else loss, "위약금": pen if "위약금_원kg" in plan else 0.0}


def _shortage(base: P.Feasibility) -> pd.DataFrame:
    return base.orders[base.orders["부족량_kg"] > 1e-6]


def _rate_step(v: float) -> float:
    """대체 비율은 5% 단위로 올린다(현장에서 배합을 바꾸기 쉬운 단위)."""
    return min(1.0, math.ceil(max(v, 0.0) * 20 - 1e-9) / 20)


def substitute_candidate(data: C.CompanyData, base: P.Feasibility, settings: C.Settings | None) -> Candidate | None:
    """부족이 난 계획 행만 '대체 배합' 제품으로 바꾼다.

    대체 비율 = 부족한 만큼(1 − 만들 수 있던 양 ÷ 계획 물량, 5% 단위 올림), 대체표의 최대 비율까지.
    원래 원료가 완전히 떨어진 날은 최대 비율로도 다 만들 수 없다(나머지 비율의 원래 원료가 필요).
    """
    if data.substitute is None or not len(data.substitute):
        return None
    short = _shortage(base)
    if short.empty:
        return None
    labels = _label_keys(data)
    recipe, plan = data.recipe.copy(), data.plan.copy()
    notes, warns, made = [], [], {}
    for _, o in short.iterrows():
        product = o["제품"]
        need = {labels[x.strip()] for x in o["부족원료"].split(",") if x.strip() in labels}
        rules = data.substitute[data.substitute["제품"].isin([product, "*"])]
        rules = rules[[(r["육종"], r["부위"], r["원산지"], r["보관"]) in need for _, r in rules.iterrows()]]
        rules = rules.drop_duplicates(["육종", "부위", "원산지", "보관"])
        if rules.empty:
            continue
        wanted = 1 - float(o["생산가능_kg"]) / float(o["계획물량_kg"])
        rates = tuple((tuple(r[c] for c in ("육종", "부위", "원산지", "보관")), min(_rate_step(wanted), float(r["최대대체비율"]) / 100), r)
                      for _, r in rules.iterrows())
        sig = (product, tuple((k, rate) for k, rate, _ in rates))
        if sig not in made:
            variant = f"{product} (대체 " + "/".join(f"{rate * 100:.0f}%" for _, rate, _ in rates) + ")"
            while variant in set(recipe["제품"]) and variant not in {v for v, _ in made.values()}:
                variant += "'"
            new_rows = []
            for _, m in recipe[recipe["제품"] == product].iterrows():
                key = tuple(m[c] for c in C.MATERIAL_KEY)
                hit = [(rate, r) for k, rate, r in rates if k == key]
                if not hit:
                    new_rows.append(dict(m, 제품=variant))
                    continue
                rate, r = hit[0]
                if rate < 1:
                    new_rows.append(dict(m, 제품=variant, 투입량_kg=float(m["투입량_kg"]) * (1 - rate)))
                sk = C.substitute_key(r)
                new_rows.append({"제품": variant, "육종": sk[0], "부위": sk[1], "원산지": sk[2], "보관": sk[3],
                                 "투입량_kg": float(m["투입량_kg"]) * rate * float(r["환산계수"])})
                notes.append(f"{variant}: {C.ingredient_label(*key)} → {C.ingredient_label(*sk)} {rate * 100:.0f}%"
                             + (f" ({r['비고']})" if str(r["비고"]) not in ("", "nan") else ""))
                if key[2] != sk[2]:
                    warns.append(f"원산지 변경({product}: {key[1]} {key[2]} → {sk[1]} {sk[2]}) — 원산지 표시·포장재 변경과 고객 승인이 필요합니다")
                if key[1] != sk[1] or key[0] != sk[0]:
                    warns.append(f"부위 변경({product}: {key[1]} → {sk[1]}) — 품질·식감·표시사항 확인이 필요합니다")
            recipe = pd.concat([recipe, pd.DataFrame(new_rows)], ignore_index=True)
            made[sig] = (variant, product)
        plan.loc[o["계획행"], "제품"] = made[sig][0]
        for k, rate, _ in rates:
            if rate + 1e-9 < _rate_step(wanted):
                notes.append(f"{product} {o['날짜']:%m/%d}: 필요한 대체 {_rate_step(wanted) * 100:.0f}% 중 최대 비율 {rate * 100:.0f}%까지만 대체 — 부족이 남습니다")
    if not made:
        return None
    cand = P.change_scenario(data, plan=plan, recipe=recipe)
    cand.substitute = data.substitute
    notes.append("부족이 난 날의 계획만, 부족한 만큼(5% 단위)만 대체 배합으로 바꿨습니다. 대체 원료 재고가 모자라면 부족이 남습니다.")
    return Candidate("대체 원료", cand, notes, sorted(set(warns)))


_VARIANT = re.compile(r"^(.*) \(대체[^)]*\)'*$")


def extra_cost(original: C.CompanyData, cand: C.CompanyData, settings: C.Settings | None = None) -> float:
    """대응안의 추가 비용(원) = 추가 매입 할증 + 배합 변경으로 바뀐 원료비. 자동 후보와 편집한 대응안이 같은 식을 쓴다.

    추가 매입 할증: 원료별 (대응안 입고 금액 − 기존 입고 금액) − 늘어난 입고량 × 기준(재구매) 단가.
                   단가를 안 적은 입고 행은 기준 단가로 본다(할증 0). 날짜만 바꾼 입고는 0원.
    배합 변경: 명시된 계획 물량 × (대응안 제품 kg당 원료비 − 원래 제품 kg당 원료비). '제품 (대체 …)'는 원래 제품과 비교한다.
    단가를 모르는 원료가 끼면 NaN.
    """
    if not K.has_prices(original):
        return math.nan
    price = {k: v for k, (v, _) in K.unit_price(K.material_prices(original), "next").items()}
    base = original.inventory["기준일"].max()

    def spend(d: C.CompanyData) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        if d.inbound is None or not len(d.inbound):
            return out
        f = d.inbound[d.inbound["입고예정일"] > base]
        for _, r in f.iterrows():
            label = C.ingredient_label(*(r[c] for c in C.MATERIAL_KEY))
            p = r.get(K.PRICE) if K.PRICE in f else math.nan
            p = price.get(label, math.nan) if p is None or pd.isna(p) else float(p)
            q, v = out.get(label, (0.0, 0.0))
            out[label] = (q + float(r["입고량_kg"]), v + float(r["입고량_kg"]) * p)
        return out

    total = 0.0
    a, b = spend(original), spend(cand)
    for label in set(a) | set(b):
        (qa, va), (qb, vb) = a.get(label, (0.0, 0.0)), b.get(label, (0.0, 0.0))
        if abs(qb - qa) < 1e-9 and abs(vb - va) < 1e-6:
            continue
        added = (qb - qa) * price.get(label, math.nan)
        total += (vb - va) - added
    # 배합 차이만 보려고 단가는 원래 데이터의 것을 쓴다(추가 입고의 할증 단가가 평균 단가에 섞이지 않게)
    same_prices = copy.copy(original)
    same_prices.recipe, same_prices.plan = cand.recipe, cand.plan
    unit_o = K.product_costs(original, "avg", None, settings).set_index("제품")["원료비_원kg"]
    unit_c = K.product_costs(same_prices, "avg", None, settings).set_index("제품")["원료비_원kg"]
    plan = cand.plan[cand.plan["날짜"] > base]
    for product, qty in plan.groupby("제품")["계획물량_kg"].sum().items():
        m = _VARIANT.match(product)
        origin = product if product in unit_o.index else (m.group(1) if m else None)
        if origin is None or origin not in unit_o.index:
            return math.nan
        total += (unit_c.get(product, math.nan) - unit_o[origin]) * float(qty)
    return total


def spot_candidate(data: C.CompanyData, base: P.Feasibility, settings: C.Settings | None, today: pd.Timestamp | None = None) -> Candidate | None:
    """부족 원료를 긴급 현물로 산다고 가정한다. 도착 전 부족은 남는다."""
    st_ = settings or C.Settings()
    m = base.materials[base.materials["미충족소요_kg"] > 1e-6]
    if m.empty:
        return None
    labels = _label_keys(data)
    basis = data.inventory["기준일"].max()
    ref = max(pd.Timestamp(today).normalize() if today is not None else basis, basis)
    price = K.unit_price(K.material_prices(data), "next") if K.has_prices(data) else {}
    rows, notes = [], []
    for label, g in m.groupby("원료"):
        key = labels[label]
        arrive = max(ref + pd.Timedelta(days=int(st_.spot_lead[key[2]])), basis + pd.Timedelta(days=1))
        thaw_days, loss = C.lot_thaw({}, key[2], key[3], st_)
        ready = (st_.next_start(arrive) or arrive) + pd.Timedelta(days=thaw_days) if thaw_days else arrive
        need = float(g.loc[g["날짜"] >= ready, "미충족소요_kg"].sum())
        before = float(g.loc[g["날짜"] < ready, "미충족소요_kg"].sum())
        if need <= 1e-6:
            notes.append(f"{label}: 긴급 현물도 {ready:%m/%d} 이후에야 쓸 수 있어 부족({before:,.0f}kg)을 줄이지 못합니다")
            continue
        qty = math.ceil(need / (1 - loss / 100) / C.ROUND_KG) * C.ROUND_KG
        p = price.get(label, (math.nan, ""))[0]
        rows.append({"입고예정일": arrive, "육종": key[0], "부위": key[1], "원산지": key[2], "보관": key[3], "입고량_kg": qty,
                     "공급처": "긴급 현물(가정)", "사용상태": "사용가능", "사용가능일": arrive if key[3] == "냉장" else pd.NaT,
                     "사용기한": pd.NaT, **({K.PRICE: p * (1 + st_.spot_premium / 100)} if not math.isnan(p) else {})})
        notes.append(f"{label}: {arrive:%m/%d} 도착 {qty:,.0f}kg (할증 {st_.spot_premium:.0f}%)" +
                     (f", 그 전 부족 {before:,.0f}kg은 남음" if before > 1e-6 else ""))
    if not rows:
        return None
    inbound = pd.concat([data.inbound, pd.DataFrame(rows)], ignore_index=True) if data.inbound is not None else pd.DataFrame(rows)
    cand = P.change_scenario(data, inbound=inbound)
    cand.substitute = data.substitute
    return Candidate("긴급 현물 구매", cand, notes, ["실제 구할 수 있는 물량·단가·도착일은 공급처 확인이 필요합니다"])


def delay_candidate(data: C.CompanyData, base: P.Feasibility, settings: C.Settings | None) -> Candidate | None:
    """원료가 모자라 부족이 난 계획 행을, 그 제품의 원료가 '모두' 들어와 있는 가장 가까운 작업일로 옮긴다(입고·해동 완료, 기한 안).

    부족 원료 하나만 보고 옮기면, 그날 다른 원료가 없거나 기한이 지나 또 부족이 날 수 있어 제품의 원료 전부를 확인한다.
    라인 능력 때문에만 부족한 행은 '생산 능력 맞춰 나누기' 후보가 다룬다.
    """
    short = _shortage(base)
    if short.empty:
        return None
    st_ = settings or C.Settings()
    labels = _label_keys(data)
    ok = Q.material_check(data, st_)
    plan = data.plan.copy()
    notes, moved, stuck = [], 0, []
    lots = [l for l in C.material_lots(data, st_) if l["status"] == "사용가능" and l["qty"] > 0]
    for _, o in short.iterrows():
        keys = [labels[x.strip()] for x in o["부족원료"].split(",") if x.strip() in labels]
        if not keys:
            continue  # 능력 부족만 있는 행
        # ① 부족 원료가 새로 들어오는(입고·해동 완료) 날 이후부터 ② 제품 원료가 모두 갖춰진 첫 작업일
        start = None
        for key in keys:
            later = [l["ready"] for l in lots if l["key"] == key and l["ready"] > o["날짜"] and l["ready"] != pd.Timestamp.max]
            if not later:
                start = None
                break
            start = max(start, min(later)) if start is not None else min(later)
        target = None
        if start is not None:
            for i in range(0, 61):
                d = start + pd.Timedelta(days=i)
                if st_.can_start(d) and ok(o["제품"], d):
                    target = d
                    break
        if target is None:
            stuck.append(f"{o['제품']} {o['날짜']:%m/%d}")
            continue
        plan.loc[o["계획행"], "날짜"] = target
        moved += 1
        late = pd.notna(o["납기"]) and target > o["납기"]
        notes.append(f"{o['제품']} {o['날짜']:%m/%d} → {target:%m/%d}" + (" (납기 넘김)" if late else ""))
    if not moved:
        return None
    if stuck:
        notes.append("새 입고 뒤 60일 안에 원료가 모두 갖춰지는 작업일이 없어 옮기지 못한 계획: " + ", ".join(stuck[:6]) + (" 외" if len(stuck) > 6 else ""))
    cand = P.change_scenario(data, plan=plan)
    cand.substitute, cand.supplier = data.substitute, data.supplier
    if Q.enabled(cand, settings):  # 옮긴 날이 라인 능력을 넘으면 앞뒤 작업일로 나눈다
        lv = Q.level(cand, settings)
        if len(lv.moves):
            cand2 = P.change_scenario(cand, plan=lv.plan)
            cand2.substitute, cand2.supplier = data.substitute, data.supplier
            cand = cand2
            notes.append(f"옮긴 날이 라인 능력을 넘어 {len(lv.moves)}건을 앞뒤 작업일로 나눴습니다")
        if len(lv.overflow):
            notes.append(f"그래도 능력을 넘는 날 {len(lv.overflow)}일은 '생산 능력' 부족으로 남습니다")
    return Candidate("생산일 늦추기", cand, notes, ["설비·인력 일정과 고객 납기 협의가 필요합니다. 옮긴 날 다른 계획과 원료를 나눠 씁니다"])


def capacity_candidate(data: C.CompanyData, base: P.Feasibility, settings: C.Settings | None) -> Candidate | None:
    """라인 하루 능력을 넘는 계획을 앞뒤 작업일로 나눈다(앞당기기 먼저, 납기 넘김 표시)."""
    if not Q.enabled(data, settings) or Q.overloads(data, settings).empty:
        return None
    lv = Q.level(data, settings)
    if lv.moves.empty:
        return None
    cand = P.change_scenario(data, plan=lv.plan)
    cand.substitute, cand.supplier = data.substitute, data.supplier
    notes = [f"{r['원래날짜']:%m/%d} {r['제품']} {r['물량_kg']:,.0f}kg → {r['옮긴날짜']:%m/%d} ({r['방향']}" + (", 납기 넘김)" if r["납기상태"] == "납기 넘김" else ")")
             for _, r in lv.moves.head(8).iterrows()]
    if len(lv.moves) > 8:
        notes.append(f"외 {len(lv.moves) - 8}건")
    if len(lv.overflow):
        notes.append("옮길 곳이 없어 능력을 넘는 날: " + ", ".join(f"{r['날짜']:%m/%d} {r['라인']} {r['초과_kg']:,.0f}kg" for _, r in lv.overflow.iterrows()))
    warns = ["앞당긴 물량은 완제품을 며칠 보관해야 합니다. 설비 점검·인력 일정도 확인하세요"]
    if (lv.moves["납기상태"] == "납기 넘김").any():
        warns.append("뒤로 미룬 물량 중 납기를 넘기는 것이 있습니다. 고객 협의가 필요합니다")
    return Candidate("생산 능력 맞춰 나누기", cand, notes, warns)


def candidates(data: C.CompanyData, settings: C.Settings | None = None, today: pd.Timestamp | None = None) -> tuple[P.Feasibility, list[Candidate]]:
    base = P.simulate(data, settings=settings)
    out = [Candidate("감산 수용", data, ["아무것도 바꾸지 않고 못 만드는 양을 그대로 둡니다."], [])]
    if _shortage(base).empty and Q.overloads(data, settings).empty:
        return base, out
    for make in (capacity_candidate, substitute_candidate, spot_candidate, delay_candidate):
        try:
            c = make(data, base, settings, today) if make is spot_candidate else make(data, base, settings)
        except ValueError:
            c = None
        if c is not None and c.data.ready():
            out.append(c)
    return base, out


def compare(data: C.CompanyData, cands: list[Candidate], settings: C.Settings | None = None) -> pd.DataFrame:
    rows = []
    end = max(c.data.plan["날짜"].max() for c in cands)
    for c in cands:
        after = P.simulate(c.data, end, settings)
        mt, mo = after.metrics(), money(c.data, after, settings)
        extra = extra_cost(data, c.data, settings)
        total = mo["감산손실"] + mo["위약금"] + extra
        rows.append({"대응안": c.name, "생산부족_kg": mt["생산부족_kg"], "영향계획건수": mt["영향계획건수"], "납기위험건수": mt["납기위험건수"],
                     "계획량_kg": mt["계획량_kg"], "남은감산손실": mo["감산손실"], "위약금": mo["위약금"], "추가비용": extra,
                     "합계": total, "경고": " / ".join(c.warnings)})
    return pd.DataFrame(rows)


LABEL_CHECKS = [
    "제품 표시사항(원산지·원재료명·함량) 변경 필요 여부 확인",
    "변경 포장재·라벨 재고와 교체 일정 확인",
    "거래처(고객) 사전 승인",
    "품질팀 확인(부위·원산지 변경에 따른 규격·식감)",
    "대체 배합을 적용한 생산일·로트 기록",
]


def _top(recipe: pd.DataFrame, product: str, n: int = 3) -> list[tuple]:
    g = recipe[recipe["제품"] == product].groupby(["육종", "부위", "원산지"])["투입량_kg"].sum().sort_values(ascending=False)
    return [k for k in g.index[:n]]


def origin_changes(original: C.CompanyData, cand: C.CompanyData) -> list[str]:
    """대응안에서 원산지 표시를 다시 봐야 하는 제품. 새 원산지가 들어오거나 배합 비율 상위 3개 원료(부위·원산지)가 바뀌면 알린다.

    실제 표시 대상·순위 기준은 관련 법규와 사내 기준을 따른다(여기서는 확인이 필요한지만 찾는다).
    """
    out = []
    base = original.inventory["기준일"].max()
    for product in sorted(set(cand.plan.loc[cand.plan["날짜"] > base, "제품"])):
        m = _VARIANT.match(product)
        origin = product if product in set(original.recipe["제품"]) else (m.group(1) if m else None)
        if origin is None:
            continue
        before, after = _top(original.recipe, origin), _top(cand.recipe, product)
        new_origin = {(k[0], k[2]) for k in after} - {(k[0], k[2]) for k in _top(original.recipe, origin, 99)}
        if before != after or new_origin:
            fmt = lambda ks: ", ".join(f"{k[1]}({k[2]})" for k in ks)  # noqa: E731
            out.append(f"{product}: 배합 상위 원료 {fmt(before)} → {fmt(after)}")
    return out
