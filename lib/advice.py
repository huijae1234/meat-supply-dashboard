"""시장 신호와 자사 계산을 합쳐 '오늘 할 일'을 만든다. 모두 규칙 기반이며 근거를 같이 보여준다.

근거 한계: 시장 데이터는 전국 단위이고 재고는 월 단위(2~3개월 지연)이다. 소비자가는 구매 단가가 아니라
참고 지표다. 자사 수량은 올린 CSV와 아래 가정(재고는 기준일 마감, 생산계획 이후는 일평균)을 따른다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lib import company as C
from lib import live
from lib import live_analysis as LA
from lib.plain import VERDICT, VERDICT_ICON, days, md, ton, vs_normal, vs_year

KIND = {"돼지": 4304, "소": 4301}
KIND_IMPORT = {"돼지": 4402, "소": 4401}
PRICE_ALERT_PCT = 10.0  # 평년 대비 이 % 이상 비싸면 참고 문구를 붙인다


@dataclass
class Market:
    """시장(공공 API) 요약. 데이터가 없으면 available=False."""
    available: bool = False
    stock_month: pd.Timestamp | None = None
    species: dict | None = None      # 육종 -> {signal, z, pct, month}
    parts: dict | None = None        # (육종, 시장 부위명) -> {signal, z, mom_pct}
    prices: pd.DataFrame | None = None
    price_date: pd.Timestamp | None = None


def market_snapshot(k: float = 1.5) -> Market:
    stk, pdl, pm = live.load("stock"), live.load("price_daily"), live.load("price_month")
    if stk.empty:
        return Market()
    species, parts = {}, {}
    month = None
    for sp, kind in KIND.items():
        if stk[stk["kind"] == kind].empty:
            continue
        sa = LA.stock_alerts(stk, kind, k).iloc[-1]
        month = sa["month"]
        species[sp] = {"signal": sa["signal"], "z": float(sa["z"]), "pct": float((sa["stock_kg"] / sa["expected_kg"] - 1) * 100),
                       "month": sa["month"], "stock_kg": float(sa["stock_kg"])}
        for _, r in LA.part_alerts(stk, kind, k).iterrows():
            parts[(sp, r["part"])] = {"signal": r["signal"], "z": float(r["z"]), "mom_pct": float(r["mom_pct"])}
    prices, pdate = None, None
    if len(pdl):
        pdate = pdl["date"].max()
        prices = LA.price_alerts(pdl, pm, live.KINDS, PRICE_ALERT_PCT)
    return Market(True, month, species, parts, prices, pdate)


def _market_for(species: str, part: str, origin: str, m: Market) -> dict:
    """원료 하나에 대응하는 시장 정보(부위 재고 신호, 가격 평년 대비)."""
    out = {"stock": None, "price": None}
    if not m.available:
        return out
    link = C.PARTS[species][part]
    out["stock"] = (m.parts or {}).get((species, link["stock"]))
    if link["price"] and m.prices is not None and len(m.prices):
        kind = KIND[species] if origin == "국산" else KIND_IMPORT[species]
        p = m.prices[m.prices["kind"] == kind]
        hit = p[p["item"].str.startswith(link["price"])]
        if len(hit):
            r = hit.iloc[0]
            out["price"] = {"item": r["item"], "gap": float(r["gap_pct"]) if r["gap_pct"] == r["gap_pct"] else None,
                            "chg30": float(r["chg30_pct"]) if r["chg30_pct"] == r["chg30_pct"] else None, "streak": r["streak"]}
    return out


def market_lines(species: str, part: str, origin: str, m: Market) -> list[str]:
    """시장 참고 문구. 우리 판단을 바꾸는 근거가 아니라 '같이 볼 것'으로만 쓴다."""
    info = _market_for(species, part, origin, m)
    lines = []
    st = info["stock"]
    if st and st["signal"] == "부족":
        lines.append(f"시장 {species} {part} 재고도 부족 신호입니다(전월 {st['mom_pct']:+.0f}%). 미루면 구하기 어려울 수 있습니다")
    elif st and st["signal"] == "과잉":
        lines.append(f"시장 {species} {part} 재고는 넉넉합니다. 단가 협상 여지가 있을 수 있습니다")
    pr = info["price"]
    if pr and pr["gap"] is not None and pr["gap"] >= PRICE_ALERT_PCT:
        tail = f"(최근 4주 연속 상승)" if pr["streak"] == "4주 연속 상승" else ""
        lines.append(f"소비자가 {vs_year(pr['gap'])}{tail}. 한 번에 몰아 사기보다 나눠 발주하는 것도 검토하세요")
    elif pr and pr["gap"] is not None and pr["gap"] <= -PRICE_ALERT_PCT:
        lines.append(f"소비자가 {vs_year(pr['gap'])}. 안전재고 위로 조금 더 확보하기 좋은 시점일 수 있습니다")
    return lines


def instruction(row: pd.Series, m: Market, ref: pd.Timestamp) -> dict:
    """원료 한 줄 -> {level, headline, steps[], market[]}. 수량·날짜까지 구체적으로 쓴다."""
    name = row["원료"]
    lead = int(row["리드타임일수"])
    steps: list[str] = []
    status = row["상태"]
    order_by = row["발주기한"]
    when = "오늘" if (order_by is not None and not pd.isna(order_by) and pd.Timestamp(order_by) <= ref) else md(order_by)

    if status == "확인필요":
        return {"level": "확인필요", "headline": f"{name}: 데이터 갱신·계산 범위 확인 필요", "steps": ["현재 발주 권고는 중단했습니다. 재고 기준일과 계산 가정을 확인하세요."], "market": []}

    if status == "긴급":
        headline = f"{name}: {md(row['소진예정일'])}에 소진되는데 가장 빠른 입고는 {md(row['가장빠른입고일'])}입니다"
        gap = row["공백량_kg"]
        steps.append(f"긴급 현물 구매 검토: 입고 전 미충족 수요 약 {ton(gap)}. 실제 공급 가능일·품질 확인 후 대응안에서 비교하세요")
        if row["입고예정_kg"] > 0:
            steps.append(f"입고 앞당기기: 이미 잡힌 입고 예정 {ton(row['입고예정_kg'])}을 공급처에 앞당겨 달라고 요청하세요")
        if row["발주량_kg"] > 0:
            steps.append(f"정규 발주 참고: 추가 조치가 없을 때 {when} 약 {ton(row['발주량_kg'])} (리드타임 {lead}일). 긴급 확보량과 단순 합산하지 말고 선택한 대응 후 재계산하세요")
        if row["영향제품"]:
            steps.append(f"생산계획 조정: 이 원료를 쓰는 제품은 {row['영향제품']}입니다. 후순위 제품 감산·연기를 검토하세요 (하루 사용 {ton(row['일평균사용_kg'])})")
        level = "긴급"
    elif status == "발주필요":
        headline = f"{name}: {when}까지 약 {ton(row['발주량_kg'])} 발주 검토"
        steps.append(f"리드타임 {lead}일을 가정한 입고일은 {md(max(pd.Timestamp(order_by), ref) + pd.Timedelta(days=lead))}입니다. 공급처 확인이 필요합니다")
        steps.append(f"현재 {ton(row['현재고_kg'])}({days(row['재고일수'])}), {md(row['안전재고이하일'])}에 안전재고({row['안전재고일수']:.0f}일치) 아래로 내려갑니다")
        if row["안전재고미달일수"] > 0:
            steps.append(f"주의: 입고 전 {int(row['안전재고미달일수'])}일 동안({md(row['안전재고이하일'])}부터) 안전재고보다 적게 운영합니다. 앞당길 수 있으면 요청하세요")
        if row["입고예정_kg"] > 0:
            steps.append(f"입고 예정 {ton(row['입고예정_kg'])}은 이미 반영한 계산입니다")
        level = "발주"
    elif status == "과잉":
        headline = f"{name}: 재고가 넉넉합니다 ({days(row['재고일수'])})"
        tail = f"입고 예정 {ton(row['입고예정_kg'])}은 일정 조정을 검토하세요" if row["입고예정_kg"] > 0 else "추가 입고 예정은 없습니다"
        steps.append(f"안전재고({row['안전재고일수']:.0f}일치)보다 많이 쌓여 있습니다. 신규 발주는 보류하세요. {tail}")
        level = "과잉"
    else:
        headline = f"{name}: 이상 없음 ({days(row['재고일수'])})"
        if pd.notna(order_by) and row["발주량_kg"] > 0:
            headline += f" · 다음 발주 기한 {md(order_by)} (약 {ton(row['발주량_kg'])})"
        level = "정상"
    return {"level": level, "headline": headline, "steps": steps,
            "market": market_lines(row["육종"], row["부위"], row["원산지"], m) if level != "정상" else []}


def company_actions(proj: C.Projection, m: Market, ref: pd.Timestamp | None = None) -> list[dict]:
    ref = ref or proj.reference_date or proj.base_date
    out = [dict(instruction(r, m, ref), ingredient=r["원료"]) for _, r in proj.summary.iterrows()]
    order = {"확인필요": -1, "긴급": 0, "발주": 1, "과잉": 2, "정상": 3}
    return sorted(out, key=lambda a: order[a["level"]])


def market_verdicts(m: Market) -> list[dict]:
    """육종별 한 줄 판정 (걱정 없소 / 이럼 안 돼지 / 넘쳐도 안 돼지)와 현장 말 설명."""
    rows = []
    for sp, v in (m.species or {}).items():
        bad_parts = [(p, i) for (s_, p), i in (m.parts or {}).items() if s_ == sp and i["signal"] == "부족"]
        bad_parts = sorted(bad_parts, key=lambda x: x[1]["z"])[:3]
        rows.append({
            "species": sp, "signal": v["signal"], "verdict": VERDICT[v["signal"]], "icon": VERDICT_ICON[v["signal"]],
            "text": f"{sp} 재고({v['month']:%Y-%m} 기준)는 {vs_normal(v['pct'])}",
            "bad_parts": [p for p, _ in bad_parts],
            "over_parts": [p for (s_, p), i in (m.parts or {}).items() if s_ == sp and i["signal"] == "과잉"][:3],
        })
    return rows
