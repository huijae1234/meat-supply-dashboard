"""명시된 생산계획만 대상으로 원료를 배분한다. 설비·인력·납품 완료를 보증하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import pandas as pd

from lib import company as C


@dataclass
class Feasibility:
    orders: pd.DataFrame
    materials: pd.DataFrame
    fingerprint: str

    def metrics(self) -> dict:
        o = self.orders
        return {
            "계획량_kg": float(o["계획물량_kg"].sum()),
            "원료기준가능_kg": float(o["생산가능_kg"].sum()),
            "생산부족_kg": float(o["부족량_kg"].sum()),
            "영향계획건수": int((o["부족량_kg"] > 1e-6).sum()),
            "납기위험건수": int(o["납기상태"].eq("위험").sum()),
            "원료부족일수": int(self.materials.loc[self.materials["미충족소요_kg"] > 1e-6, "날짜"].nunique()),
            "마지막날사용가능재고_kg": float(self.materials.loc[self.materials["날짜"] == self.materials["날짜"].max(), "사용가능잔량_kg"].sum()),
        }


def fingerprint(data: C.CompanyData) -> str:
    content = "\n".join(getattr(data, k).to_json(date_format="iso", orient="split", force_ascii=False)
                        for k in C.FILES if getattr(data, k) is not None)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def simulate(data: C.CompanyData, end_date=None) -> Feasibility:
    if not data.ready():
        raise ValueError("입력 오류를 먼저 수정해 주세요.")
    base = data.inventory["기준일"].max()
    plan = data.plan[data.plan["날짜"] > base].copy()
    if plan.empty:
        raise ValueError("재고 기준일 다음 날 이후의 생산계획이 필요합니다. 과거 계획을 자동 연장하지 않습니다.")
    end = max(plan["날짜"].max(), pd.Timestamp(end_date)) if end_date is not None else plan["날짜"].max()
    if (end - base).days > 366:
        raise ValueError("비교 기간은 재고 기준일부터 최대 366일입니다.")
    plan["계획행"] = plan.index
    plan = plan.sort_values(["날짜", "우선순위", "납기", "계획행"], kind="stable", na_position="last")
    recipes = {}
    for product, group in data.recipe.groupby("제품"):
        recipes[product] = group.groupby(C.MATERIAL_KEY)["투입량_kg"].sum().to_dict()
    lots = C.material_lots(data)
    keys = sorted(set(lot["key"] for lot in lots) | {key for rec in recipes.values() for key in rec})
    orders, material_days = [], []
    for day in pd.date_range(base + pd.Timedelta(days=1), end):
        wanted, used = {k: 0.0 for k in keys}, {k: 0.0 for k in keys}
        for _, row in plan[plan["날짜"] == day].iterrows():
            recipe = recipes[row["제품"]]
            quantity = float(row["계획물량_kg"])
            capacity = {key: sum(lot["qty"] for lot in C.available_lots(lots, key, day)) / ratio for key, ratio in recipe.items()}
            possible = max(0.0, min([quantity, *capacity.values()]))
            shortages = [C.ingredient_label(*key) for key, cap in capacity.items() if cap + 1e-6 < quantity]
            for key, ratio in recipe.items():
                wanted[key] += quantity * ratio
                used[key] += possible * ratio
                C.take(lots, key, day, possible * ratio)
            due = row["납기"]
            risk = "미입력" if pd.isna(due) else ("위험" if day > due or quantity - possible > 1e-6 else "원료상 충족")
            orders.append({"계획행": int(row["계획행"]), "주문번호": row["주문번호"], "날짜": day, "제품": row["제품"],
                           "납기": due, "우선순위": row["우선순위"], "계획물량_kg": quantity, "생산가능_kg": possible,
                           "부족량_kg": quantity - possible, "부족원료": ", ".join(shortages), "납기상태": risk})
        for key in keys:
            material_days.append({"날짜": day, "원료": C.ingredient_label(*key), "계획소요_kg": wanted[key],
                                  "실현가능투입_kg": used[key], "미충족소요_kg": max(0.0, wanted[key] - used[key]),
                                  "사용가능잔량_kg": sum(lot["qty"] for lot in C.available_lots(lots, key, day))})
    return Feasibility(pd.DataFrame(orders), pd.DataFrame(material_days), fingerprint(data))


def change_scenario(data: C.CompanyData, plan=None, inbound=None) -> C.CompanyData:
    """화면에서 편집한 복사본만 검증한다. 원본 CSV와 실제 주문은 변경하지 않는다."""
    raw = {k: getattr(data, k).copy(deep=True) if getattr(data, k) is not None else None for k in C.FILES}
    if plan is not None:
        raw["plan"] = plan
    if inbound is not None:
        raw["inbound"] = inbound
    result = C.load_data(raw)
    if result.ready():
        base = result.inventory["기준일"].max()
        if result.plan["날짜"].le(base).any():
            result.issues.append(C.Issue("오류", "생산계획", "대응안의 생산일은 재고 기준일 다음 날 이후여야 합니다."))
        if result.inbound is not None and result.inbound["입고예정일"].le(base).any():
            result.issues.append(C.Issue("오류", "입고 예정", "대응안 입고일은 재고 기준일 다음 날 이후여야 합니다."))
    return result


def comparison(before: C.CompanyData, after: C.CompanyData) -> tuple[Feasibility, Feasibility, pd.DataFrame]:
    if not before.ready() or not after.ready():
        raise ValueError("입력 오류를 먼저 수정해 주세요.")
    # 같은 기간을 비교한다. 계획 삭제·감산도 숨기지 않고 계획량 차이로 표시한다.
    end = max(before.plan["날짜"].max(), after.plan["날짜"].max())
    a, b = simulate(before, end), simulate(after, end)
    av, bv = a.metrics(), b.metrics()
    table = pd.DataFrame([{"지표": key, "기존": value, "대응안": bv[key], "변화": bv[key] - value} for key, value in av.items()])
    return a, b, table


def snapshot(data: C.CompanyData) -> str:
    return json.dumps({k: json.loads(getattr(data, k).to_json(orient="records", date_format="iso", force_ascii=False))
                       for k in C.FILES if getattr(data, k) is not None}, ensure_ascii=False)
