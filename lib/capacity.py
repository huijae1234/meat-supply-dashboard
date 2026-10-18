"""생산 라인 하루 능력: 능력을 넘는 날을 찾고, 넘치는 양을 앞뒤 작업일로 나눈 안을 만든다.

규칙
  - 라인 = 생산계획의 '라인' 열, 없으면 화면에서 정한 제품 -> 라인. 라인 하루 최대 생산량(kg)이 없거나 0이면 제한 없음.
  - 작업일 = 해동 계획과 같은 달력(투입 요일·휴무일).
  - 넘치는 양은 우선순위 숫자가 큰(덜 급한) 계획부터 옮긴다.
  - 옮길 곳: ① 앞당기기 — 최대 N작업일 전까지, 가까운 날부터. 그날까지 그 제품의 원료가 모두 들어와 있어야 한다(입고·해동 완료).
            ② 뒤로 미루기 — 최대 N작업일 뒤까지, 가까운 날부터. 납기를 넘기면 '납기 넘김'으로 표시한다.
            남은 능력만큼만 채운다. 그래도 남으면 '능력 초과'로 원래 날짜에 둔다.
  - 원료 '수량'이 충분한지는 여기서 보지 않는다. 나눈 안을 생산 가능량 계산(planning.simulate)에 넣어 함께 확인한다.
  - 원본 CSV는 바꾸지 않는다. 조정된 생산계획은 내려받아 사람이 확인 후 다시 올린다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from lib import company as C

EPS = 1e-6


@dataclass
class Leveling:
    plan: pd.DataFrame      # 조정된 생산계획(원본 열 그대로, 나눈 행은 같은 주문번호로 추가)
    moves: pd.DataFrame     # 옮긴 내역
    overflow: pd.DataFrame  # 옮기지 못한 초과
    load: pd.DataFrame      # 라인·날짜별 조정 전후 생산량과 능력


MOVE_COLS = ["원래날짜", "제품", "주문번호", "라인", "옮긴날짜", "물량_kg", "방향", "납기", "납기상태"]


def _lines(data: C.CompanyData, st_: C.Settings) -> pd.Series:
    row_line = data.plan["라인"] if "라인" in data.plan else pd.Series(None, index=data.plan.index)
    return pd.Series([st_.line_of(p, l) for p, l in zip(data.plan["제품"], row_line)], index=data.plan.index)


def enabled(data: C.CompanyData, settings: C.Settings | None = None) -> bool:
    st_ = settings or C.Settings()
    return any(st_.capacity_of(l) > 0 for l in set(_lines(data, st_).dropna()))


def overloads(data: C.CompanyData, settings: C.Settings | None = None) -> pd.DataFrame:
    """라인·날짜별 계획량이 하루 능력을 넘는 날."""
    st_ = settings or C.Settings()
    base = data.inventory["기준일"].max()
    p = data.plan.assign(_line=_lines(data, st_))
    p = p[(p["날짜"] > base) & p["_line"].notna()]
    g = p.groupby(["_line", "날짜"], as_index=False)["계획물량_kg"].sum()
    g["능력_kg"] = g["_line"].map(st_.capacity_of)
    g = g[(g["능력_kg"] > 0) & (g["계획물량_kg"] > g["능력_kg"] + EPS)]
    return g.rename(columns={"_line": "라인", "계획물량_kg": "계획량_kg"}).assign(초과_kg=lambda x: x["계획량_kg"] - x["능력_kg"]).reset_index(drop=True)


def _ready_dates(data: C.CompanyData, st_: C.Settings) -> dict[tuple, list]:
    out: dict[tuple, list] = {}
    for lot in C.material_lots(data, st_):
        if lot["status"] == "사용가능" and lot["qty"] > EPS:
            out.setdefault(lot["key"], []).append((lot["ready"], lot["expires"]))
    return out


def level(data: C.CompanyData, settings: C.Settings | None = None) -> Leveling:
    st_ = settings or C.Settings()
    base = data.inventory["기준일"].max()
    plan = data.plan.copy()
    plan["_line"] = _lines(data, st_)
    future = plan["날짜"] > base
    load: dict[tuple, float] = plan[future & plan["_line"].notna()].groupby(["_line", "날짜"])["계획물량_kg"].sum().to_dict()
    before = dict(load)
    recipes = {p: [tuple(k) for k in g[C.MATERIAL_KEY].itertuples(index=False, name=None)] for p, g in data.recipe.groupby("제품")}
    ready = _ready_dates(data, st_)

    def materials_in(product: str, day: pd.Timestamp) -> bool:
        return all(any(r <= day and (pd.isna(e) or day <= e) for r, e in ready.get(k, [])) for k in recipes.get(product, []))

    def workdays(day: pd.Timestamp, step: int) -> list[pd.Timestamp]:
        out, d = [], day
        for _ in range(60):
            if len(out) >= st_.shift_days:
                break
            d = d + pd.Timedelta(days=step)
            if step < 0 and d <= base:
                break
            if st_.can_start(d):
                out.append(d)
        return out

    moves, overflow, added = [], [], []
    for line in sorted({l for l in plan["_line"].dropna()}):
        cap = st_.capacity_of(line)
        if cap <= 0:
            continue
        for day in sorted(d for (l, d) in before if l == line):
            excess = load.get((line, day), 0.0) - cap
            if excess <= EPS:
                continue
            rows = plan[future & (plan["_line"] == line) & (plan["날짜"] == day) & (plan["계획물량_kg"] > EPS)]
            rows = rows.assign(_due=rows["납기"].fillna(pd.Timestamp.max)).sort_values(["우선순위", "_due", "계획물량_kg"], ascending=[False, False, False])
            for idx, row in rows.iterrows():
                if excess <= EPS:
                    break
                targets = [(d, "앞당김") for d in workdays(day, -1) if materials_in(row["제품"], d)] + [(d, "미룸") for d in workdays(day, +1)]
                for t, how in targets:
                    room = cap - load.get((line, t), 0.0)
                    amt = min(excess, float(plan.at[idx, "계획물량_kg"]), room)
                    if amt <= EPS:
                        continue
                    plan.at[idx, "계획물량_kg"] -= amt
                    load[(line, day)] -= amt
                    load[(line, t)] = load.get((line, t), 0.0) + amt
                    excess -= amt
                    new = row.drop(labels=["_due"]).to_dict()
                    new.update({"날짜": t, "계획물량_kg": amt})
                    added.append(new)
                    due = row["납기"]
                    moves.append({"원래날짜": day, "제품": row["제품"], "주문번호": row["주문번호"], "라인": line, "옮긴날짜": t,
                                  "물량_kg": amt, "방향": how, "납기": due,
                                  "납기상태": "미입력" if pd.isna(due) else ("납기 넘김" if t > due else "납기 안")})
                    if excess <= EPS:
                        break
            if excess > EPS:
                overflow.append({"날짜": day, "라인": line, "초과_kg": excess, "능력_kg": cap})
    out = pd.concat([plan, pd.DataFrame(added)], ignore_index=True) if added else plan
    out = out[out["계획물량_kg"] > EPS].drop(columns=["_line"]).sort_values(["날짜", "제품"], kind="stable").reset_index(drop=True)
    rows = [{"라인": l, "날짜": d, "조정 전_kg": before.get((l, d), 0.0), "조정 후_kg": load.get((l, d), 0.0), "능력_kg": st_.capacity_of(l)}
            for (l, d) in sorted(set(before) | set(load)) if st_.capacity_of(l) > 0]
    return Leveling(out, pd.DataFrame(moves, columns=MOVE_COLS), pd.DataFrame(overflow, columns=["날짜", "라인", "초과_kg", "능력_kg"]), pd.DataFrame(rows))
