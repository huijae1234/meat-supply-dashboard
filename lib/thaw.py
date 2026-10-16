"""해동 계획: 냉동 원료를 언제, 어느 로트에서, 얼마나 해동실에 넣어야 하는지 계산한다.

규칙 (company.material_lots와 같은 기준)
  - 배합표 투입량은 해동 후 무게다. 해동실에 넣을 냉동 무게 = 필요량 ÷ (1 − 감량률).
  - 사용가능일을 적은 냉동 로트는 이미 해동 중(또는 해동 일정이 잡힌) 로트로 보고 그날부터 쓴다. 새 해동 일정을 잡지 않는다.
  - 사용가능일이 없는 냉동 로트는 들어온 날(재고는 기준일 다음 날)부터 해동을 시작할 수 있고, 해동일수가 지나야 쓴다.
  - 해동 시작은 '쓰는 날 − 해동일수'(가장 늦게 넣어도 되는 날)로 잡는다. 그날이 휴무일·비작업 요일이면 그 전 작업일로 당긴다.
  - 해동실 하루 용량을 넘으면 넘는 양을 그 전 작업일로 앞당긴다.
  - 앞당겨진 만큼 해동 후 냉장 대기가 생긴다. 대기 일수가 보관 한도를 넘는 투입은 잡지 않고 문제로 알린다.
  - 명시된 생산계획만 쓴다(계획 뒤 평균 연장 없음). 사용기한이 빠른 로트부터 쓴다.
  - 해동 후 냉장 보관 가능 기간, 해동 방법(유수·냉장·고주파), 해동 안전성 판정은 다루지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from lib import company as C

EPS = 1e-6


@dataclass
class ThawPlan:
    events: pd.DataFrame    # 해동 투입 한 건 = 한 줄
    problems: pd.DataFrame  # 해동으로 맞추지 못하는 필요량
    capacity: int
    base_date: pd.Timestamp

    def load(self) -> pd.DataFrame:
        """해동 시작일별 냉동 투입량."""
        if self.events.empty:
            return pd.DataFrame(columns=["해동시작일", "원료", "냉동투입_kg"])
        return self.events.groupby(["해동시작일", "원료"], as_index=False)["냉동투입_kg"].sum()


EVENT_COLS = ["해동시작일", "사용일", "원료", "로트", "냉동투입_kg", "해동후_kg", "해동일수", "감량률", "대기일수", "상태"]
PROBLEM_COLS = ["날짜", "원료", "부족_kg", "이유"]


def frozen_demand(data: C.CompanyData) -> dict[tuple, pd.Series]:
    """냉동 원료별 날짜별 필요량(해동 후 kg). 명시된 생산계획만."""
    base = data.inventory["기준일"].max()
    plan = data.plan[data.plan["날짜"] > base]
    rec = data.recipe[data.recipe["보관"] == "냉동"]
    out: dict[tuple, pd.Series] = {}
    for _, r in rec.groupby(["제품"] + C.MATERIAL_KEY, as_index=False)["투입량_kg"].sum().iterrows():
        p = plan[plan["제품"] == r["제품"]].groupby("날짜")["계획물량_kg"].sum() * r["투입량_kg"]
        if p.empty:
            continue
        key = tuple(r[c] for c in C.MATERIAL_KEY)
        out[key] = out[key].add(p, fill_value=0.0) if key in out else p
    return out


def _usable(lot: dict, day: pd.Timestamp) -> bool:
    return lot["status"] == "사용가능" and lot["qty"] > EPS and (pd.isna(lot["expires"]) or day <= lot["expires"])


def _order(lot: dict):
    return (lot["expires"] if pd.notna(lot["expires"]) else pd.Timestamp.max, lot["arrival"], lot["id"])


def latest_start(st_: C.Settings, lo: pd.Timestamp, hi: pd.Timestamp) -> pd.Timestamp | None:
    """lo~hi 사이에서 해동실에 넣을 수 있는 가장 늦은 날."""
    d = hi
    while d >= lo:
        if st_.can_start(d):
            return d
        d -= pd.Timedelta(days=1)
    return None


def _start_for(st_: C.Settings, lot: dict, day: pd.Timestamp, first: pd.Timestamp) -> tuple[pd.Timestamp | None, str]:
    """(해동 시작일, 못 쓰는 이유). 대기 = (쓰는 날 − 해동일수) − 시작일 이 보관 한도 이내여야 한다."""
    want = day - pd.Timedelta(days=lot["thaw_days"])
    lo = max(lot["arrival"], first)
    if want < lo:
        return None, "time"
    s = latest_start(st_, lo, want)
    if s is None:
        return None, "workday"
    if (want - s).days > st_.thaw_hold:
        return None, "hold"
    return s, ""


REASONS = {
    "time": "해동 시간 부족: 냉동 재고는 있지만 지금 넣어도 이날까지 해동이 끝나지 않습니다",
    "workday": "해동 투입일 없음: 들어온 뒤 쓰는 날까지 해동실에 넣을 수 있는 작업일이 없습니다",
    "hold": "해동 후 보관 한도 초과: 휴무일 때문에 너무 일찍 넣어야 해서 쓰기 전 냉장 대기가 한도를 넘습니다",
    "stock": "냉동 재고 부족: 쓸 수 있는 로트가 없습니다('원료 소요·재고'의 발주 검토 참고)",
}


def plan(data: C.CompanyData, settings: C.Settings | None = None) -> ThawPlan:
    if not data.ready():
        raise ValueError("입력 오류를 먼저 수정해 주세요.")
    st_ = settings or C.Settings()
    base = data.inventory["기준일"].max()
    first = base + pd.Timedelta(days=1)
    lots = [dict(lot) for lot in C.material_lots(data, st_) if lot["key"][3] == "냉동"]
    events, problems = [], []
    for key, need_by_day in sorted(frozen_demand(data).items()):
        label = C.ingredient_label(*key)
        mine = [lot for lot in lots if lot["key"] == key]
        for day, need in need_by_day.sort_index().items():
            need = float(need)
            # 1) 이미 해동 중인 로트(사용가능일 입력)부터
            for lot in sorted((l for l in mine if l["explicit_ready"] and l["ready"] <= day and _usable(l, day)), key=_order):
                used = min(lot["qty"], need)
                lot["qty"] -= used
                need -= used
                if need <= EPS:
                    break
            # 2) 해동을 새로 잡을 로트: 들어온 날 + 해동일수 <= 쓰는 날
            why = set()
            if need > EPS:
                for lot in sorted((l for l in mine if not l["explicit_ready"] and _usable(l, day) and l["arrival"] <= day), key=_order):
                    start, bad = _start_for(st_, lot, day, first)
                    if start is None:
                        why.add(bad)
                        continue
                    used = min(lot["qty"], need)
                    lot["qty"] -= used
                    need -= used
                    wait = (day - pd.Timedelta(days=lot["thaw_days"]) - start).days
                    events.append({"해동시작일": start, "사용일": day, "원료": label, "로트": lot["id"],
                                   "냉동투입_kg": used / (1 - lot["loss"] / 100), "해동후_kg": used, "해동일수": lot["thaw_days"],
                                   "감량률": lot["loss"], "대기일수": wait, "상태": "주말·휴무일 때문에 앞당김" if wait else "정상",
                                   "_earliest": max(lot["arrival"], first)})
                    if need <= EPS:
                        break
            if need > EPS:
                reason = next((REASONS[r] for r in ("time", "workday", "hold") if r in why), REASONS["stock"])
                problems.append({"날짜": day, "원료": label, "부족_kg": need, "이유": reason})
    cap = int(st_.thaw_capacity)
    if cap > 0 and events:
        events, overflow = _fit_capacity(events, cap, first, st_)
        problems += overflow
    ev = pd.DataFrame(events, columns=EVENT_COLS + ["_earliest"]).drop(columns="_earliest")
    if len(ev):
        ev = ev.sort_values(["해동시작일", "원료", "사용일", "로트"]).reset_index(drop=True)
    pr = pd.DataFrame(problems, columns=PROBLEM_COLS)
    if len(pr):
        pr = pr.sort_values(["날짜", "원료"]).reset_index(drop=True)
    return ThawPlan(ev, pr, cap, base)


def _fit_capacity(events: list[dict], cap: int, first_day: pd.Timestamp, st_: C.Settings) -> tuple[list[dict], list[dict]]:
    """하루 냉동 투입량이 용량을 넘으면, 넘는 양을 그 전 작업일로 옮긴다(로트가 그날 이미 있고 보관 한도 이내). 늦은 날부터 처리한다."""
    overflow = []
    day = max(e["해동시작일"] for e in events)
    while day >= first_day:
        todays = [e for e in events if e["해동시작일"] == day]
        excess = sum(e["냉동투입_kg"] for e in todays) - cap
        prev = latest_start(st_, first_day, day - pd.Timedelta(days=1)) or first_day - pd.Timedelta(days=1)
        # 쓰는 날이 먼 것부터 앞당긴다 (대기 시간이 덜 늘어나는 쪽은 아니지만, 가까운 날 쓸 물량을 지킨다)
        for e in sorted(todays, key=lambda e: (e["사용일"], e["로트"]), reverse=True):
            if excess <= EPS:
                break
            gap = (day - prev).days
            if prev < first_day or prev < e["_earliest"] or e["대기일수"] + gap > st_.thaw_hold:
                continue
            move = min(excess, e["냉동투입_kg"])
            thawed = e["해동후_kg"] * move / e["냉동투입_kg"]
            moved = dict(e, 해동시작일=prev, 냉동투입_kg=move, 해동후_kg=thawed,
                         대기일수=e["대기일수"] + gap, 상태="용량 때문에 앞당김")
            e["냉동투입_kg"] -= move
            e["해동후_kg"] -= thawed
            events.append(moved)
            excess -= move
        events = [e for e in events if e["냉동투입_kg"] > EPS]
        if excess > EPS:
            overflow.append({"날짜": day, "원료": ", ".join(sorted({e["원료"] for e in events if e["해동시작일"] == day})),
                             "부족_kg": excess, "이유": f"해동실 용량 초과: 이날 투입이 용량({cap:,}kg)보다 {excess:,.0f}kg 많고 앞당길 수 없습니다(로트 미입고·보관 한도)"})
            for e in events:
                if e["해동시작일"] == day and e["상태"] == "정상":
                    e["상태"] = "용량 초과"
        day -= pd.Timedelta(days=1)
    return events, overflow


def next_start(tp: ThawPlan, today: pd.Timestamp) -> tuple[pd.Timestamp | None, pd.DataFrame]:
    """판단일 이후 가장 가까운 해동 투입일과 그날 목록."""
    if tp.events.empty:
        return None, tp.events
    ev = tp.events[tp.events["해동시작일"] >= max(pd.Timestamp(today).normalize(), tp.base_date + pd.Timedelta(days=1))]
    if ev.empty:
        return None, ev
    day = ev["해동시작일"].min()
    return day, ev[ev["해동시작일"] == day]
