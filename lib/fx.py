"""환율(한국수출입은행 현재환율 API) 수집·누적과 변화율. 수입 원료는 모두 USD 결제로 본다.

API: https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON?authkey=…&searchdate=YYYYMMDD&data=AP01
  - 응답은 통화별 목록. result 1 성공 / 2 데이터 코드 오류 / 3 인증 코드 오류 / 4 하루 호출 한도 마감
  - 주말·공휴일은 빈 목록, 당일은 오전 11시 무렵 고시 전까지 빈 목록이다
  - 하루 1,000건 제한. 여유를 남겨 한 번 실행에 최대 FX_BUDGET건만 부른다
저장: data/live/fx.csv (date, cur_unit, deal_bas_r) — 매매기준율. 빈 날짜(휴일)는 _fx_state.json에 적어 다시 부르지 않는다.
"""
from __future__ import annotations

import json
import math
import os
from datetime import timedelta

import pandas as pd
import requests

from lib import live

URL = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
FX_FILE = "fx.csv"
STATE_FILE = "_fx_state.json"
FX_BUDGET = 300
CURRENCY = "USD"
KEY_ENV = "KEY_KOREAEXIM"


class FxError(RuntimeError):
    """인증 오류·한도 마감처럼 더 부르면 안 되는 오류."""


def has_key() -> bool:
    return bool(os.environ.get(KEY_ENV))


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return math.nan


def fetch(day: pd.Timestamp, session=requests) -> pd.DataFrame:
    """하루치 환율. 빈 목록이면 빈 표(휴일·고시 전)."""
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise FxError(f".env 에 {KEY_ENV} 가 없습니다.")
    r = session.get(URL, params={"authkey": key, "searchdate": f"{day:%Y%m%d}", "data": "AP01"}, timeout=20)
    r.raise_for_status()
    rows = r.json() or []
    codes = {int(x.get("result", 1)) for x in rows}
    if 3 in codes:
        raise FxError("환율 API 인증 코드 오류입니다. .env의 KEY_KOREAEXIM을 확인하세요.")
    if 4 in codes:
        raise FxError("환율 API 하루 호출 한도가 마감됐습니다. 내일 이어서 받습니다.")
    if 2 in codes:
        raise FxError("환율 API 데이터 코드 오류입니다.")
    return pd.DataFrame([{"date": day.normalize(), "cur_unit": x["cur_unit"], "deal_bas_r": _num(x.get("deal_bas_r"))}
                         for x in rows if x.get("cur_unit")])


def _state() -> dict:
    p = live.LIVE_DIR / STATE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"empty": []}


def load() -> pd.DataFrame:
    p = live.LIVE_DIR / FX_FILE
    if not p.exists():
        return pd.DataFrame(columns=["date", "cur_unit", "deal_bas_r"])
    df = pd.read_csv(p, parse_dates=["date"])
    return df.sort_values(["date", "cur_unit"]).reset_index(drop=True)


def collect(days_back: int = 180, budget: int = FX_BUDGET, today: pd.Timestamp | None = None, session=requests) -> dict:
    """빠진 영업일을 최신 날짜부터 받는다. 이미 받은 날짜·휴일로 확인한 날짜는 건너뛴다."""
    live.LIVE_DIR.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp(today or pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)).normalize()
    have = load()
    got = set(have["date"].dt.normalize()) if len(have) else set()
    state = _state()
    empty = set(state.get("empty", []))
    todo = [today - timedelta(days=i) for i in range(days_back + 1)]
    todo = [d for d in todo if d.weekday() < 5 and d not in got and f"{d:%Y-%m-%d}" not in empty]
    calls, new, stopped, error = 0, [], False, ""
    for d in todo:
        if calls >= budget:
            stopped = True
            break
        try:
            df = fetch(d, session)
        except FxError as e:
            error = str(e)
            break
        except Exception as e:  # 네트워크 등 개별 실패: 다음 실행에서 다시
            error = f"{type(e).__name__}: {e}".replace(os.environ.get(KEY_ENV, "") or "\0", "***")
            break
        calls += 1
        if df.empty:
            if d < today:  # 오늘은 고시 전일 수 있어 다음에 다시 본다
                empty.add(f"{d:%Y-%m-%d}")
            continue
        new.append(df)
    if new:
        out = pd.concat([have, *new], ignore_index=True).drop_duplicates(["date", "cur_unit"], keep="last")
        out.sort_values(["date", "cur_unit"]).to_csv(live.LIVE_DIR / FX_FILE, index=False, date_format="%Y-%m-%d")
    state["empty"] = sorted(empty)
    state["last"] = {"at": str(pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).floor("s")), "calls": calls,
                     "new_days": len(new), "error": error, "pending": max(len(todo) - calls, 0) if (stopped or error) else 0}
    (live.LIVE_DIR / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state["last"]


def last_status() -> dict:
    return _state().get("last", {})


def series(cur: str = CURRENCY, df: pd.DataFrame | None = None) -> pd.Series:
    df = load() if df is None else df
    s = df[df["cur_unit"] == cur].set_index("date")["deal_bas_r"].dropna().sort_index()
    return s


def latest(cur: str = CURRENCY, df: pd.DataFrame | None = None) -> tuple[pd.Timestamp | None, float]:
    s = series(cur, df)
    return (s.index[-1], float(s.iloc[-1])) if len(s) else (None, math.nan)


def change(cur: str = CURRENCY, days: int = 30, df: pd.DataFrame | None = None) -> float:
    """최근 고시 vs days일 전(그날 또는 그 전 마지막 고시) 변화율(%)."""
    s = series(cur, df)
    if len(s) < 2:
        return math.nan
    ago = s[s.index <= s.index[-1] - pd.Timedelta(days=days)]
    return (float(s.iloc[-1]) / float(ago.iloc[-1]) - 1) * 100 if len(ago) else math.nan
