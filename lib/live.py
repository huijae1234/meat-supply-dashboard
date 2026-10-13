"""실제 공공 API 수집기 (축산물품질평가원). 호출 결과를 data/live/*.csv 에 누적 저장한다.

사용 API (모두 http://data.ekape.or.kr/openapi-data/service/user/grade/ 하위, XML)
  LPStock            부위별 월 재고         (돼지: 톤, 소: kg -> kg로 통일)
  consumerPriceDaily 일별 소비자가격         (원/100g -> 원/kg)
  consumerPriceMonth 월별 소비자가격+평년가격
  auct/beefGrade     소 부분육 경락가격(전 도매시장 평균)
  auct/pigJejuGrade  제주 돼지도체 등급별 경락가격

개발계정 한도 하루 1,000건이라 호출 수를 data/live/_state.json 에 기록하고 DAILY_BUDGET 에서 멈춘다.
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = ROOT / "data" / "live"
BASE = "http://data.ekape.or.kr/openapi-data/service/user/grade/"
DAILY_BUDGET = 900  # 한도 1,000건 중 여유를 남긴다
KINDS = {4301: "소(한우·육우)", 4304: "돼지", 4401: "수입 소고기", 4402: "수입 돼지고기"}
STOCK_PARTS = {
    4301: ["안심", "등심", "채끝", "목심", "앞다리", "우둔", "설도", "사태", "갈비", "양지", "특수부위", "잡육"],
    4304: ["안심", "등심", "전지", "후지", "삼겹살", "목심", "갈비", "특수부위", "잡육"],
}

load_dotenv(ROOT / ".env")


SKIPPED = "__skipped__"


class BudgetExceeded(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


# ------------------------------------------------------------------ 상태 / 호출 예산
def _state_path() -> Path:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return LIVE_DIR / "_state.json"


def _read_state() -> dict:
    p = _state_path()
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    today = date.today().isoformat()
    if st.get("calls_date") != today:
        st["calls_date"], st["calls_today"] = today, 0
    st.setdefault("checked", {})
    return st


def _write_state(st: dict) -> None:
    _state_path().write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def calls_today() -> int:
    return _read_state()["calls_today"]


def last_refresh() -> str | None:
    return _read_state().get("last_refresh")


def has_key() -> bool:
    return bool(os.environ.get("KEY_EKAPE_DISTRIB") or os.environ.get("KEY_AT_PRICE"))


def _key() -> str:
    k = os.environ.get("KEY_EKAPE_DISTRIB") or os.environ.get("KEY_AT_PRICE") or ""
    if not k:
        raise ApiError(".env 에 KEY_EKAPE_DISTRIB 가 없습니다.")
    return k  # 이미 URL 인코딩된 키: 그대로 URL 에 붙인다 (requests 가 다시 인코딩하면 인증 실패)


# ------------------------------------------------------------------ 저수준 호출
def _call(path: str, **params) -> list[dict]:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}{path}?serviceKey={_key()}&{qs}"
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            code = (root.findtext("./header/resultCode") or "").strip()
            if code not in ("00", "03"):  # 03 = 데이터 없음
                raise ApiError(f"{path} {code}: {root.findtext('./header/resultMsg')}")
            return [{c.tag: (c.text or "").strip() for c in it} for it in root.iterfind("./body/items/item")]
        except (requests.RequestException, ET.ParseError) as e:
            last = e
    raise ApiError(f"{path} 호출 실패: {last}")


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


# ------------------------------------------------------------------ 테이블별 수집
def fetch_stock(year: int, month: int, kind: int) -> pd.DataFrame:
    rows = _call("LPStock", standYyyy=year, standMm=month, judgeKind=kind)
    out = []
    for it in rows:
        mult = 1000 if it.get("unit", "").lower() == "ton" else 1  # 돼지는 톤 -> kg
        for i, part in enumerate(STOCK_PARTS[kind], start=1):
            v = _num(it.get(f"livestockPart_{i}"))
            if v is not None:
                out.append({"month": f"{year}-{month:02d}-01", "kind": kind, "part": part, "stock_kg": v * mult})
        out.append({"month": f"{year}-{month:02d}-01", "kind": kind, "part": "합계", "stock_kg": (_num(it.get("totStock")) or 0) * mult})
    return pd.DataFrame(out)


def fetch_price_daily(ymd: str, kind: int) -> pd.DataFrame:
    rows = _call("consumerPriceDaily", standYmd=ymd, judgeKind=kind)
    out = [{"date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "kind": kind, "item": r["itemNm"], "grade": r.get("grdNm", ""),
            "price_krw_kg": (_num(r["ntslPrc"]) or 0) * 10, "max_krw_kg": (_num(r.get("maxPrc")) or 0) * 10,
            "min_krw_kg": (_num(r.get("minPrc")) or 0) * 10} for r in rows if _num(r.get("ntslPrc"))]
    return pd.DataFrame(out)


def fetch_price_month(ym: str, kind: int) -> pd.DataFrame:
    rows = _call("consumerPriceMonth", standYm=ym, judgeKind=kind)
    out = [{"month": f"{ym[:4]}-{ym[4:]}-01", "kind": kind, "item": r["itemNm"], "grade": r.get("grdNm", ""),
            "price_krw_kg": (_num(r["ntslPrc"]) or 0) * 10, "normal_krw_kg": (_num(r.get("avgYearPrc")) or 0) * 10} for r in rows if _num(r.get("ntslPrc"))]
    return pd.DataFrame(out)


def fetch_auction_beef(ymd: str) -> pd.DataFrame:
    rows = _call("auct/beefGrade", startYmd=ymd, endYmd=ymd)
    d = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    out = [{"date": d, "cut": r["cutmeatName"], "han_krw_kg": _num(r.get("hanAvgT")), "han_boxes": _num(r.get("hanBoxCnt")),
            "yuk_krw_kg": _num(r.get("yukAvgT")), "yuk_boxes": _num(r.get("yukBoxCnt"))} for r in rows]
    df = pd.DataFrame(out)
    return df[(df["han_boxes"].fillna(0) + df["yuk_boxes"].fillna(0)) > 0] if len(df) else df  # 거래 없는 부위 제외


def fetch_auction_pig(ymd: str) -> pd.DataFrame:
    rows = _call("auct/pigJejuGrade", startYmd=ymd, endYmd=ymd)
    d = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    out = [{"date": d, "grade": r["gradeName"], "price_krw_kg": _num(r.get("totPrice")), "heads": _num(r.get("totCnt"))} for r in rows]
    df = pd.DataFrame(out)
    return df[df["heads"].fillna(0) > 0] if len(df) else df


# ------------------------------------------------------------------ 저장 / 읽기
SCHEMAS = {
    "stock": ["month", "kind", "part"],
    "price_daily": ["date", "kind", "item", "grade"],
    "price_month": ["month", "kind", "item", "grade"],
    "auction_beef": ["date", "cut"],
    "auction_pig": ["date", "grade"],
}


def load(name: str) -> pd.DataFrame:
    p = LIVE_DIR / f"{name}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    for c in ("date", "month"):
        if c in df:
            df[c] = pd.to_datetime(df[c])
    return df


def _upsert(name: str, new: pd.DataFrame) -> None:
    if new is None or new.empty:
        return
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    p = LIVE_DIR / f"{name}.csv"
    if p.exists():
        new = pd.concat([pd.read_csv(p), new], ignore_index=True)
    new = new.drop_duplicates(subset=SCHEMAS[name], keep="last").sort_values(SCHEMAS[name][0])
    new.to_csv(p, index=False, encoding="utf-8")


# ------------------------------------------------------------------ 갱신
def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def refresh(days_back: int = 60, stock_from: str = "2024-01", progress=None) -> dict:
    """아직 못 받은 날짜·월만 호출해서 누적 저장. 예산 초과 시 중단하고 여태 받은 것은 보존한다."""
    st = _read_state()
    checked = st["checked"]
    log = {"calls": 0, "new_rows": {}, "errors": [], "stopped_by_budget": False}
    today = date.today()

    # (api, 키, 호출 함수) 작업 목록: 이미 받은 것은 건너뛴다. 최신 2개월 재고, 최근 2일 가격은 다시 받는다.
    jobs = []
    sy, sm = map(int, stock_from.split("-"))
    for y, m in _months(date(sy, sm, 1), today):
        recent = (today.year * 12 + today.month) - (y * 12 + m) <= 1
        for kind in (4301, 4304):
            key = f"{y}{m:02d}-{kind}"
            if recent or key not in checked.get("stock", []):
                jobs.append(("stock", key, lambda y=y, m=m, kind=kind: fetch_stock(y, m, kind), date(y, m, 28)))
    for i in range(days_back, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:  # 주말은 경매·가격 조사가 없다
            continue
        ymd = d.strftime("%Y%m%d")
        fresh = i <= 1
        for kind in KINDS:
            key = f"{ymd}-{kind}"
            if fresh or key not in checked.get("price_daily", []):
                jobs.append(("price_daily", key, lambda ymd=ymd, kind=kind: fetch_price_daily(ymd, kind), d))
        for api, fn in (("auction_beef", fetch_auction_beef), ("auction_pig", fetch_auction_pig)):
            if fresh or ymd not in checked.get(api, []):
                jobs.append((api, ymd, lambda ymd=ymd, fn=fn: fn(ymd), d))
    for y, m in _months(date(today.year - 1, today.month, 1), today):
        for kind in KINDS:
            key = f"{y}{m:02d}-{kind}"
            recent = (today.year * 12 + today.month) - (y * 12 + m) <= 1
            if recent or key not in checked.get("price_month", []):
                jobs.append(("price_month", key, lambda y=y, m=m, kind=kind: fetch_price_month(f"{y}{m:02d}", kind), date(y, m, 28)))

    jobs.sort(key=lambda j: j[3], reverse=True)  # 한도에 걸리면 오래된 것부터 포기 (최신 우선)
    room = DAILY_BUDGET - st["calls_today"]
    log["pending"] = 0
    if len(jobs) > room:
        log["pending"] = len(jobs) - max(room, 0)  # 오늘 한도 때문에 못 받은 작업 수 (다음 실행에서 이어서 받는다)
        jobs, log["stopped_by_budget"] = jobs[:max(room, 0)], True

    fatal = {"msg": None}  # 키·권한 오류는 모든 호출이 똑같이 실패하므로 즉시 중단한다
    FATAL_WORDS = ("서비스키", "ACCESS_DENIED", "활용기간", "등록되지 않은", "요청제한")

    def run(job):
        api, key, fn, _ = job
        if fatal["msg"]:
            return api, key, None, SKIPPED
        try:
            return api, key, fn(), None
        except Exception as e:  # 개별 실패는 기록하고 계속
            if any(w in str(e) for w in FATAL_WORDS):
                fatal["msg"] = str(e)
            return api, key, None, f"{api} {key}: {e}"

    buf: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for n, (api, key, df, err) in enumerate(ex.map(run, jobs), start=1):
            if err == SKIPPED:
                log["pending"] += 1
                continue
            log["calls"] += 1
            if err:
                log["errors"].append(err)
            else:
                buf.setdefault(api, []).append(df)
                checked.setdefault(api, [])
                if key not in checked[api]:
                    checked[api].append(key)
            if progress:
                progress(n, len(jobs))
    for api, dfs in buf.items():
        dfs = [d for d in dfs if d is not None and not d.empty]
        if dfs:
            merged = pd.concat(dfs, ignore_index=True)
            _upsert(api, merged)
            log["new_rows"][api] = len(merged)
    st["calls_today"] += log["calls"]
    st["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    st["last_status"] = {"ok": not log["errors"], "errors": len(log["errors"]), "calls": log["calls"],
                         "pending": log["pending"], "at": st["last_refresh"]}
    _write_state(st)
    if log["errors"]:
        with open(LIVE_DIR / "_errors.log", "a", encoding="utf-8") as f:
            for e in log["errors"]:
                f.write(st["last_refresh"] + " | " + e + chr(10))
    return log


def last_status() -> dict:
    return _read_state().get("last_status", {})


def recent_errors(n: int = 10) -> list[str]:
    p = LIVE_DIR / "_errors.log"
    return p.read_text(encoding="utf-8").strip().splitlines()[-n:] if p.exists() else []


# ------------------------------------------------------------------ 품질 점검
def quality_check(jump_pct: float = 30.0) -> list[dict]:
    """수집된 데이터의 이상을 찾는다: 가격 급변, 평일 결측, 재고 급변, 단위 이상."""
    issues: list[dict] = []
    pdl = load("price_daily")
    if len(pdl):
        pdl = pdl.sort_values("date")
        for (kind, item, grade), g in pdl.groupby(["kind", "item", "grade"]):
            ch = g["price_krw_kg"].pct_change() * 100
            for idx in ch[ch.abs() >= jump_pct].index:
                issues.append({"level": "경고", "table": "price_daily", "when": g.loc[idx, "date"].strftime("%Y-%m-%d"),
                               "detail": f"{KINDS.get(kind, kind)} {item}({grade}) 전일 대비 {ch[idx]:+.0f}%"})
        # 평일 결측: 가격 데이터가 있는 구간 안에서 한 종류라도 빠진 평일 (공휴일일 수 있어 '참고')
        days = set(pdl["date"].dt.strftime("%Y-%m-%d"))
        rng = pd.bdate_range(pdl["date"].min(), pdl["date"].max())
        missing = [d.strftime("%Y-%m-%d") for d in rng if d.strftime("%Y-%m-%d") not in days]
        if missing:
            issues.append({"level": "참고", "table": "price_daily", "when": f"{len(missing)}일",
                           "detail": "가격이 없는 평일 (공휴일일 수 있음): " + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")})
        # 새로 들어온 날짜 안에서 종류별 행 수 편차 (일부 부위가 빠졌는지)
        cnt = pdl.groupby(["date", "kind"]).size().unstack(fill_value=0)
        if len(cnt):
            last = cnt.iloc[-1]
            typical = cnt.median()
            for kind in cnt.columns:
                if typical[kind] and last[kind] < typical[kind] * 0.6:
                    issues.append({"level": "경고", "table": "price_daily", "when": cnt.index[-1].strftime("%Y-%m-%d"),
                                   "detail": f"{KINDS.get(kind, kind)} 최신일 행 수 {last[kind]}개 (평소 {typical[kind]:.0f}개)"})
    stk = load("stock")
    if len(stk):
        for kind, g in stk[stk["part"] == "합계"].groupby("kind"):
            g = g.sort_values("month")
            ch = g["stock_kg"].pct_change() * 100
            for idx in ch[ch.abs() >= 40].index:
                issues.append({"level": "경고", "table": "stock", "when": g.loc[idx, "month"].strftime("%Y-%m"),
                               "detail": f"{KINDS.get(kind, kind)} 재고 전월 대비 {ch[idx]:+.0f}% (단위 오류 가능성)"})
            exp = pd.date_range(g["month"].min(), g["month"].max(), freq="MS")
            gap = [m.strftime("%Y-%m") for m in exp if m not in set(g["month"])]
            if gap:
                issues.append({"level": "경고", "table": "stock", "when": ",".join(gap[:5]), "detail": f"{KINDS.get(kind, kind)} 재고 월 누락"})
    ab = load("auction_beef")
    if len(ab):
        t = ab[ab["cut"] == "소계"].sort_values("date")
        ch = t["han_krw_kg"].pct_change() * 100
        for idx in ch[ch.abs() >= jump_pct].index:
            issues.append({"level": "경고", "table": "auction_beef", "when": t.loc[idx, "date"].strftime("%Y-%m-%d"),
                           "detail": f"한우 소계 경락가 전일 대비 {ch[idx]:+.0f}%"})
    (LIVE_DIR / "_quality.json").write_text(json.dumps(issues, ensure_ascii=False), encoding="utf-8")
    return issues


def load_quality() -> list[dict]:
    p = LIVE_DIR / "_quality.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    res = refresh(days_back=n)
    res["quality_issues"] = len(quality_check())
    print(json.dumps(res, ensure_ascii=False, indent=1))
