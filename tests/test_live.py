"""실제 API 수집기: 파싱, 단위 환산, 호출 예산(최신 우선), 키 오류 즉시 중단."""
import pandas as pd
import pytest

from lib import live

XML_OK = """<?xml version="1.0" encoding="UTF-8"?><response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>
<body><items><item><a>1</a><b> x </b></item><item><a>2</a><b>y</b></item></items></body></response>"""
XML_NODATA = "<response><header><resultCode>03</resultCode><resultMsg>NODATA</resultMsg></header><body><items/></body></response>"
XML_BADKEY = "<response><header><resultCode>99</resultCode><resultMsg>등록되지 않은 서비스키</resultMsg></header></response>"
FETCHERS = ("fetch_stock", "fetch_price_daily", "fetch_price_month", "fetch_auction_beef", "fetch_auction_pig")


class FakeResp:
    def __init__(self, text):
        self.text, self.status_code = text, 200

    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    monkeypatch.setenv("KEY_EKAPE_DISTRIB", "K%2B")
    return tmp_path


def test_call_parses_items_and_strips(monkeypatch):
    monkeypatch.setattr(live.requests, "get", lambda url, timeout: FakeResp(XML_OK))
    assert live._call("x") == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]


def test_call_nodata_is_empty_not_error(monkeypatch):
    monkeypatch.setattr(live.requests, "get", lambda url, timeout: FakeResp(XML_NODATA))
    assert live._call("x") == []


def test_call_error_code_raises(monkeypatch):
    monkeypatch.setattr(live.requests, "get", lambda url, timeout: FakeResp(XML_BADKEY))
    with pytest.raises(live.ApiError, match="서비스키"):
        live._call("x")


def test_key_is_not_reencoded(monkeypatch):
    seen = []
    monkeypatch.setattr(live.requests, "get", lambda url, timeout: seen.append(url) or FakeResp(XML_OK))
    live._call("x", a=1)
    assert "serviceKey=K%2B&" in seen[0]  # 이미 인코딩된 키를 그대로 사용


def test_stock_pork_ton_converted_to_kg(monkeypatch):
    row = {"unit": "ton", "totStock": "15", **{f"livestockPart_{i}": "1" for i in range(1, 10)}}
    monkeypatch.setattr(live, "_call", lambda *a, **k: [row])
    df = live.fetch_stock(2026, 6, 4304)
    assert df.loc[df["part"] == "합계", "stock_kg"].iloc[0] == 15_000
    assert {"삼겹살", "후지", "합계"} <= set(df["part"])


def test_stock_beef_kept_in_kg(monkeypatch):
    row = {"unit": "kg", "totStock": "500", **{f"livestockPart_{i}": "10" for i in range(1, 13)}}
    monkeypatch.setattr(live, "_call", lambda *a, **k: [row])
    df = live.fetch_stock(2026, 6, 4301)
    assert df.loc[df["part"] == "합계", "stock_kg"].iloc[0] == 500


def test_price_unit_100g_to_kg(monkeypatch):
    monkeypatch.setattr(live, "_call", lambda *a, **k: [
        {"itemNm": "삼겹살", "grdNm": "-", "ntslPrc": "2,719", "maxPrc": "3000", "minPrc": "2500"}])
    df = live.fetch_price_daily("20261007", 4304)
    assert df["price_krw_kg"].iloc[0] == 27190  # 원/100g x 10


def test_budget_keeps_newest_dates(monkeypatch):
    called = []
    monkeypatch.setattr(live, "DAILY_BUDGET", 30)
    for name in FETCHERS:
        monkeypatch.setattr(live, name, lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(live, "fetch_price_daily", lambda ymd, kind: called.append(ymd) or pd.DataFrame())
    res = live.refresh(days_back=60, stock_from="2026-10")
    assert res["stopped_by_budget"] and res["pending"] > 0
    assert called
    weekdays = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=45)
    assert weekdays[0].strftime("%Y%m%d") not in called  # 가장 오래된 날짜는 한도 때문에 포기
    assert weekdays[-1].strftime("%Y%m%d") in called  # 가장 최근 평일은 반드시 받는다


def test_auth_error_stops_remaining_calls(monkeypatch):
    attempts = []

    def bad(*a, **k):
        attempts.append(1)
        raise live.ApiError("LPStock 99: 등록되지 않은 서비스키")

    for name in FETCHERS:
        monkeypatch.setattr(live, name, bad)
    res = live.refresh(days_back=30, stock_from="2026-01")
    assert len(attempts) <= 8  # 병렬 4개 + 여유. 수백 건을 다 두드리지 않는다
    assert res["pending"] > 50
    assert not live.last_status()["ok"]


def test_checked_dates_are_skipped_next_run(monkeypatch):
    for name in FETCHERS:
        monkeypatch.setattr(live, name, lambda *a, **k: pd.DataFrame())
    first = live.refresh(days_back=10, stock_from="2026-09")
    second = live.refresh(days_back=10, stock_from="2026-09")
    assert second["calls"] < first["calls"]  # 이미 받은 날짜는 다시 호출하지 않음
