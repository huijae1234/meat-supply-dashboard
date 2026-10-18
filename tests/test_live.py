"""실제 API 수집기: 파싱, 단위 환산, 호출 예산(최신 우선), 키 오류 즉시 중단."""
import json

import pandas as pd
import pytest
import requests

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


def test_call_does_not_echo_key_or_url_from_connection_error(monkeypatch):
    def fail(url, timeout):
        raise requests.ConnectionError(f"Failed to establish connection: {url}")

    monkeypatch.setattr(live.requests, "get", fail)
    with pytest.raises(live.ApiError) as caught:
        live._call("LPStock")
    assert "연결 오류" in str(caught.value)
    assert "K%2B" not in str(caught.value)
    assert "http://" not in str(caught.value)


def test_call_preserves_http_status_without_key_or_url(monkeypatch):
    class HttpResp:
        status_code = 403

        def raise_for_status(self):
            raise requests.HTTPError(f"403 for {self.url}", response=self)

    attempts = []

    def fail(url, timeout):
        attempts.append(1)
        result = HttpResp()
        result.url = url
        return result

    monkeypatch.setattr(live.requests, "get", fail)
    with pytest.raises(live.ApiError) as caught:
        live._call("LPStock")
    assert "HTTP 403" in str(caught.value)
    assert caught.value.fatal
    assert len(attempts) == 1  # 권한 오류는 재시도해 호출 한도를 낭비하지 않는다
    assert "K%2B" not in str(caught.value)
    assert "http://" not in str(caught.value)


def test_call_does_not_echo_key_in_api_response_or_xml_error(monkeypatch):
    xml = "<response><header><resultCode>99</resultCode><resultMsg>등록되지 않은 서비스키 K%2B https://example.test/leak</resultMsg></header></response>"
    monkeypatch.setattr(live.requests, "get", lambda url, timeout: FakeResp(xml))
    with pytest.raises(live.ApiError) as caught:
        live._call("LPStock")
    assert caught.value.fatal
    assert "서비스키" in str(caught.value)
    assert "K%2B" not in str(caught.value)
    assert "https://" not in str(caught.value)

    monkeypatch.setattr(live.requests, "get", lambda url, timeout: FakeResp("<invalid K%2B"))
    with pytest.raises(live.ApiError) as caught:
        live._call("LPStock")
    assert "XML 해석 오류" in str(caught.value)
    assert "K%2B" not in str(caught.value)


def test_english_service_key_error_is_fatal(monkeypatch):
    xml = "<response><header><resultCode>30</resultCode><resultMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</resultMsg></header></response>"
    monkeypatch.setattr(live.requests, "get", lambda url, timeout: FakeResp(xml))
    with pytest.raises(live.ApiError) as caught:
        live._call("LPStock")
    assert caught.value.fatal
    assert "서비스키" in str(caught.value)


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


def test_upsert_replaces_complete_csv_and_preserves_old_file_on_failure(monkeypatch):
    original = pd.DataFrame([{"month": "2026-01-01", "kind": 4304, "part": "삼겹살", "stock_kg": 100}])
    update = pd.DataFrame([{"month": "2026-01-01", "kind": 4304, "part": "삼겹살", "stock_kg": 120}])
    path = live.LIVE_DIR / "stock.csv"
    original.to_csv(path, index=False, encoding="utf-8")
    before = path.read_bytes()

    def interrupted_write(self, target, *args, **kwargs):
        target.write("partial CSV")
        raise OSError("disk full while writing")

    with monkeypatch.context() as patcher:
        patcher.setattr(pd.DataFrame, "to_csv", interrupted_write)
        with pytest.raises(OSError, match="disk full"):
            live._upsert("stock", update)
    assert path.read_bytes() == before
    assert not list(live.LIVE_DIR.glob(".stock.*.tmp"))

    live._upsert("stock", update)
    saved = pd.read_csv(path)
    assert len(saved) == 1
    assert saved.loc[0, "stock_kg"] == 120


def test_write_state_replaces_complete_json_and_preserves_old_file_on_failure(monkeypatch):
    path = live.LIVE_DIR / "_state.json"
    live._write_state({"calls_today": 4, "checked": {"stock": ["202601-4304"]}})
    before = path.read_bytes()

    def interrupted_replace(source, destination):
        raise OSError("replace interrupted")

    with monkeypatch.context() as patcher:
        patcher.setattr(live.os, "replace", interrupted_replace)
        with pytest.raises(OSError, match="replace interrupted"):
            live._write_state({"calls_today": 5, "checked": {"stock": ["202601-4304"]}})
    assert path.read_bytes() == before
    assert not list(live.LIVE_DIR.glob("._state.*.tmp"))

    live._write_state({"calls_today": 5, "checked": {"stock": ["202601-4304"]}})
    assert json.loads(path.read_text(encoding="utf-8"))["calls_today"] == 5


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


def test_refresh_max_calls_limits_this_run(monkeypatch):
    for name in FETCHERS:
        monkeypatch.setattr(live, name, lambda *a, **k: pd.DataFrame())
    result = live.refresh(days_back=30, stock_from="2026-01", max_calls=3)
    assert result["calls"] == 3
    assert result["stopped_by_budget"]
    assert result["pending"] > 0


def test_refresh_public_log_omits_secret_and_legacy_lines(monkeypatch):
    secret_error = "등록되지 않은 서비스키 K%2B: http://example.test/?serviceKey=K%2B"

    def fail(*args, **kwargs):
        raise live.ApiError(secret_error)

    for name in FETCHERS:
        monkeypatch.setattr(live, name, fail)
    (live.LIVE_DIR / "_errors.log").write_text(f"legacy | {secret_error}\n", encoding="utf-8")
    result = live.refresh(days_back=3, stock_from="2026-10", max_calls=4)
    assert result["errors"]
    assert "K%2B" not in str(result["errors"])
    assert "http://" not in str(result["errors"])
    public_errors = live.recent_errors()
    assert public_errors
    assert all("[safe-v1]" in line for line in public_errors)
    assert "K%2B" not in str(public_errors)
    assert "http://" not in str(public_errors)


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
