"""경보 알림: 수집 직후 규칙을 평가하고, 새로 생긴 경보만 Windows 알림·로그·이력으로 남긴다.

규칙
  stock   월 재고(합계)가 추세+계절 기대값에서 ±stock_k σ 이상 벗어남, 부위별 z 가 ±part_z 이상
  price   부위 소비자가가 평년 대비 ±price_gap % 이상, 또는 4주 연속 상승/하락
  quality 데이터 품질 점검의 '경고'
  collect 마지막 수집이 실패
  company 우리 공장 CSV 계산 결과가 긴급·발주 필요 (올려 둔 파일이 있을 때만)
중복 방지: data/live/_alerts.json 에 (규칙·대상·기간) 키를 저장해 같은 경보는 다시 보내지 않는다.
  python -m lib.alerts          # 실제 평가 + 발송
  python -m lib.alerts --test   # 임계값을 낮춰 시험 (별도 이력 파일 사용, 실제 이력은 건드리지 않음)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from lib import advice as ADV
from lib import company as CO
from lib import live
from lib import live_analysis as LA

CONFIG = {"stock_k": 1.5, "part_z": 2.5, "price_gap": 15.0}
TEST_CONFIG = {"stock_k": 0.5, "part_z": 1.0, "price_gap": 5.0}
KIND_KO = {4304: "돼지", 4301: "소"}


def _history_path(test: bool) -> Path:
    return live.LIVE_DIR / ("_alerts_test.json" if test else "_alerts.json")


def load_history(test: bool = False, n: int | None = None) -> list[dict]:
    p = _history_path(test)
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    return rows[-n:][::-1] if n else rows


def _h(*parts) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:10]


def find_alerts(cfg: dict) -> list[dict]:
    """현재 조건을 만족하는 모든 경보. key 는 같은 상황에서 항상 같은 값."""
    out: list[dict] = []
    stk, pdl, pm = live.load("stock"), live.load("price_daily"), live.load("price_month")

    if len(stk):
        for kind, ko in KIND_KO.items():
            if not len(stk[stk["kind"] == kind]):
                continue
            sa = LA.stock_alerts(stk, kind, cfg["stock_k"])
            last = sa.iloc[-1]
            if last["signal"] != "정상":
                out.append({"rule": "stock", "level": "경고", "key": f"stock:{kind}:{last['month']:%Y-%m}",
                            "message": f"{ko} 재고 {last['month']:%Y-%m} {last['signal']} (z={last['z']:+.2f})"})
            pa = LA.part_alerts(stk, kind, cfg["part_z"])
            for _, r in pa[pa["signal"] != "정상"].iterrows():
                out.append({"rule": "stock", "level": "주의", "key": f"part:{kind}:{r['part']}:{stk['month'].max():%Y-%m}",
                            "message": f"{ko} {r['part']} 재고 {r['signal']} (z={r['z']:+.1f}, 전월 {r['mom_pct']:+.0f}%)"})

    if len(pdl):
        wk = pdl["date"].max().isocalendar()
        for _, r in LA.price_alerts(pdl, pm, live.KINDS, cfg["price_gap"]).iterrows():
            if r["alert"] == "":
                continue
            ym = pdl["date"].max().strftime("%Y-%m")
            if abs(r["gap_pct"]) >= cfg["price_gap"] if r["gap_pct"] == r["gap_pct"] else False:
                out.append({"rule": "price", "level": "주의", "key": f"gap:{r['kind']}:{r['item']}:{ym}",
                            "message": f"{r['종류']} {r['item']} 소비자가 평년 대비 {r['gap_pct']:+.0f}% ({r['latest_krw_kg']:,.0f}원/kg)"})
            if r["streak"]:
                out.append({"rule": "price", "level": "주의", "key": f"streak:{r['kind']}:{r['item']}:{wk[0]}-{wk[1]}",
                            "message": f"{r['종류']} {r['item']} 소비자가 {r['streak']}"})

    for q in live.load_quality():
        if q["level"] == "경고":
            out.append({"rule": "quality", "level": "주의", "key": f"quality:{q['table']}:{_h(q['when'], q['detail'])}",
                        "message": f"데이터 품질: {q['detail']} ({q['when']})"})

    # 환율: USD가 30일 사이 기준(설정) 이상 움직이면. 올린 공장 파일이 있으면 수입 원료 원료비 영향도 붙인다
    try:
        from lib import fx as FX, cost as CS
        chg, (day, rate) = FX.change("USD"), FX.latest("USD")
        st_ = CO.load_settings()
        if day is not None and chg == chg and abs(chg) >= st_.fx_alert_pct:
            extra = ""
            saved_fx = CO.load_saved()
            if any(v is not None for v in saved_fx.values()):
                d_fx = CO.load_data(saved_fx)
                if d_fx.ready() and CS.has_prices(d_fx):
                    pc = CS.product_costs(d_fx, "avg", {k: v for k, (v, _) in CS.fx_changes(d_fx, st_).items()}, st_)
                    ok = pc[pc["단가없는원료"] == ""]
                    diff = float((ok["가정계획원료비"] - ok["계획원료비"]).sum())
                    if abs(diff) >= 1:
                        extra = f" · 수입 원료 계획 기간 원료비 {diff:+,.0f}원"
            out.append({"rule": "fx", "level": "주의", "key": f"fx:USD:{day:%Y-%m-%d}:{'up' if chg > 0 else 'down'}",
                        "message": f"환율 · USD {rate:,.1f}원({day:%m/%d}), 30일 {chg:+.1f}%{extra}"})
    except Exception:
        pass

    # 우리 공장: 올려 둔 CSV가 있을 때만 (샘플 데이터는 알리지 않는다). 기본값(안전재고·리드타임)은 코드 기본값을 쓴다.
    try:
        saved = CO.load_saved()
        if any(v is not None for v in saved.values()):
            data = CO.load_data(saved)
            if data.ready():
                from lib import orders as OR, worklog as WL
                data = OR.apply_pending(data, OR.pending_inbound(WL.read_events(), data, "올려 둔 파일"))  # 발주 완료로 기록한 물량은 알리지 않는다
                proj = CO.project(data, today=CO.today_kst(), settings=CO.load_settings())
                if not proj.actionable:
                    out.append({"rule": "company", "level": "주의", "key": f"company-stale:{proj.base_date:%Y-%m-%d}",
                                "message": "우리 공장 · 재고 데이터 갱신 필요. 발주 권고를 중단했습니다."})
                if proj.actionable:  # 소비기한: 계획대로 써도 기한까지 다 못 쓰는 로트
                    from lib import expiry as EX
                    for _, w in EX.analyze(data, CO.load_settings(), CO.today_kst()).waste.iterrows():
                        out.append({"rule": "company", "level": "주의",
                                    "key": f"company-expiry:{w['원료']}:{w['로트']}:{pd.Timestamp(w['유효기한']):%Y-%m-%d}",
                                    "message": f"우리 공장 · {w['원료']} 로트 {w['로트']}: {pd.Timestamp(w['유효기한']):%m/%d} 기한까지 "
                                               f"{w['폐기예상_kg']:,.0f}kg 남을 예정(폐기 위험)"})
                for _, r in proj.summary.iterrows():
                    if r["상태"] in ("긴급", "발주필요"):
                        deadline = r["발주기한"] if r["발주기한"] is not None and not pd.isna(r["발주기한"]) else r["소진예정일"]
                        ins = ADV.instruction(r, ADV.Market(), proj.reference_date)
                        out.append({"rule": "company", "level": "경고" if r["상태"] == "긴급" else "주의",
                                    "key": f"company:{r['원료']}:{r['상태']}:{pd.Timestamp(deadline):%Y-%m-%d}",
                                    "message": "우리 공장 · " + ins["headline"]})
            else:
                out.append({"rule": "company", "level": "주의", "key": "company-invalid-input",
                            "message": "우리 공장 · 입력 오류로 계산을 중단했습니다. 데이터 점검 항목을 확인하세요."})
    except Exception as e:  # 자사 파일 문제로 시장 알림까지 막지 않는다
        out.append({"rule": "collect", "level": "주의", "key": f"company-error:{type(e).__name__}:{str(e)[:40]}",
                    "message": f"우리 공장 파일을 계산하지 못했습니다: {e}"})

    status = live.last_status()
    if status and not status.get("ok", True):
        out.append({"rule": "collect", "level": "경고", "key": f"collect:{status.get('at')}",
                    "message": f"수집 실패 {status.get('errors')}건 ({status.get('at')})"})
    return out


def _toast(title: str, body: str) -> bool:
    """Windows 알림. 비밀번호·별도 설치가 필요 없고, 실패해도 로그·이력은 남는다."""
    if os.name != "nt":
        return False
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:ALERT_TITLE)) | Out-Null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:ALERT_BODY)) | Out-Null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('축산물 수급 대시보드').Show($n)"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=30,
                           env={**os.environ, "ALERT_TITLE": title, "ALERT_BODY": body})
        return r.returncode == 0
    except Exception:
        return False


def evaluate(test: bool = False, notify: bool = True) -> dict:
    """규칙 평가 -> 신규 경보만 기록·발송. 결과 요약을 반환한다."""
    cfg = TEST_CONFIG if test else CONFIG
    history = load_history(test)
    seen = {h["key"] for h in history}
    found = find_alerts(cfg)
    new = [a for a in found if a["key"] not in seen]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    delivered = False
    if new:
        for a in new:
            a["at"] = now
        if notify:
            top = sorted(new, key=lambda a: a["level"] != "경고")[:3]
            body = " / ".join(a["message"] for a in top) + (f" 외 {len(new) - 3}건" if len(new) > 3 else "")
            delivered = _toast(f"수급 경보 {len(new)}건" + (" (시험)" if test else ""), body)
        for a in new:
            a["delivered"] = delivered
        live.LIVE_DIR.mkdir(parents=True, exist_ok=True)
        _history_path(test).write_text(json.dumps(history + new, ensure_ascii=False), encoding="utf-8")
        with open(live.LIVE_DIR / ("alerts_test.log" if test else "alerts.log"), "a", encoding="utf-8") as f:
            for a in new:
                f.write(f"{now} [{a['level']}] {a['message']}" + chr(10))
    return {"found": len(found), "new": len(new), "delivered": delivered, "messages": [a["message"] for a in new]}


if __name__ == "__main__":
    res = evaluate(test="--test" in sys.argv)
    print(json.dumps(res, ensure_ascii=False, indent=1))
