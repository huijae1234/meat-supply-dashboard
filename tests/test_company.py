"""자사(공장) 데이터: CSV 검증, 소진·발주 계산, 지시 문구, 알림 규칙."""
import io

import pandas as pd
import pytest

from lib import advice as ADV
from lib import alerts
from lib import company as C
from lib import live
from lib.plain import deviation_word, md, ton, vs_normal, vs_year

BASE = "2026-01-01"


def _tables(stock=1000, per_day=100, days=14, safety=2, lead=2, cycle=3, inbound=None, origin="국산", storage="냉장"):
    """앞다리 한 가지, 제품 '햄'이 하루 per_day kg을 만들고 배합 1.0(원료 1kg/제품 1kg)."""
    t = pd.Timestamp(BASE)
    inv = pd.DataFrame([[BASE, "돼지", "앞다리", origin, storage, stock, safety, lead, cycle]],
                       columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "안전재고일수", "리드타임일수", "발주주기일"])
    plan = pd.DataFrame([[(t + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), "햄", per_day] for i in range(1, days + 1)],
                        columns=["날짜", "제품", "계획물량_kg"])
    rec = pd.DataFrame([["햄", "돼지", "앞다리", origin, 1.0]], columns=["제품", "육종", "부위", "원산지", "투입량_kg"])
    out = {"inventory": inv, "plan": plan, "recipe": rec, "inbound": None}
    if inbound:
        out["inbound"] = pd.DataFrame([[d, "돼지", "앞다리", origin, q] for d, q in inbound], columns=["입고예정일", "육종", "부위", "원산지", "입고량_kg"])
    return out


def _row(**kw):
    data = C.load_data(_tables(**kw))
    assert data.ready(), data.issues
    return C.project(data).summary.iloc[0]


# ---------------------------------------------------------------- CSV 읽기·검증
def test_cp949_csv_is_read():
    raw = "기준일,육종,부위,원산지,보관,재고량_kg\n2026-01-01,돼지,앞다리,국산,냉장,1000\n".encode("cp949")
    df = C.read_csv_bytes(raw)
    assert df["부위"].iloc[0] == "앞다리"


def test_utf8_bom_csv_is_read():
    raw = "기준일,육종,부위,원산지,보관,재고량_kg\n2026-01-01,돼지,앞다리,국산,냉장,1000\n".encode("utf-8-sig")
    assert list(C.read_csv_bytes(raw).columns)[0] == "기준일"


def test_unknown_part_is_error():
    issues: list[C.Issue] = []
    df = pd.DataFrame([[BASE, "돼지", "꼬리", "국산", "냉장", 10]], columns=C.REQUIRED["inventory"])
    assert C.clean_table("inventory", df, issues) is None
    assert issues and issues[0].level == "오류" and "꼬리" in issues[0].message


def test_missing_required_column_is_error():
    issues: list[C.Issue] = []
    df = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", "냉장"]], columns=["기준일", "육종", "부위", "원산지", "보관"])
    assert C.clean_table("inventory", df, issues) is None
    assert "재고량_kg" in issues[0].message


def test_aliases_and_loose_headers():
    issues: list[C.Issue] = []
    df = pd.DataFrame([[BASE, "돼지고기", "전지", "수입산", "냉동", "1,500"]], columns=["기준일", "육종", "부위", "원산지", "보관", "재고량(kg)"])
    out = C.clean_table("inventory", df, issues)
    assert out is not None, issues
    r = out.iloc[0]
    assert (r["육종"], r["부위"], r["원산지"], r["재고량_kg"]) == ("돼지", "앞다리", "수입", 1500)


def test_negative_quantity_is_error():
    issues: list[C.Issue] = []
    df = pd.DataFrame([[BASE, "돼지", "앞다리", "국산", "냉장", -5]], columns=C.REQUIRED["inventory"])
    assert C.clean_table("inventory", df, issues) is None


def test_product_without_recipe_blocks_calculation():
    t = _tables()
    t["plan"] = pd.concat([t["plan"], pd.DataFrame([[t["plan"]["날짜"].iloc[0], "신제품", 50]], columns=t["plan"].columns)])
    d = C.load_data(t)
    assert any("신제품" in i.message and i.level == "오류" for i in d.issues)
    assert not d.ready()


def test_required_file_missing_not_ready():
    t = _tables()
    t["recipe"] = None
    assert not C.load_data(t).ready()


# ---------------------------------------------------------------- 소진·발주 계산
def test_runout_safety_breach_and_order_quantity():
    r = _row(stock=1000, per_day=100, safety=2, lead=2, cycle=10)
    assert r["상태"] == "발주필요"
    assert pd.Timestamp(r["소진예정일"]) == pd.Timestamp("2026-01-12")      # 11일째에 음수
    assert pd.Timestamp(r["안전재고이하일"]) == pd.Timestamp("2026-01-10")   # 재고 100 < 안전재고 200
    assert pd.Timestamp(r["발주기한"]) == pd.Timestamp("2026-01-08")         # 안전재고 이하일 - 리드타임
    assert r["발주량_kg"] == 1100                                          # (안전재고 200 + 주기 10일x100) - 도착 시점 재고 100
    assert r["재고일수"] == pytest.approx(10.0)


def test_emergency_when_runout_before_earliest_arrival():
    r = _row(stock=150, per_day=100, safety=1, lead=5)
    assert r["상태"] == "긴급"
    assert r["공백량_kg"] == pytest.approx(250)  # 입고 전(4일차)까지 -250
    assert pd.Timestamp(r["가장빠른입고일"]) == pd.Timestamp("2026-01-06")


def test_order_far_in_future_is_not_a_todo():
    """발주 기한이 이번 발주 주기(3일)보다 멀면 '할 일'로 올리지 않는다. 기한만 알려 준다."""
    r = _row(stock=1000, per_day=100, safety=2, lead=2, cycle=3)
    assert r["상태"] == "정상"
    assert pd.Timestamp(r["발주기한"]) == pd.Timestamp("2026-01-08") and r["발주량_kg"] > 0


def test_order_within_cycle_is_a_todo():
    r = _row(stock=500, per_day=100, safety=2, lead=2, cycle=3)
    assert r["상태"] == "발주필요"
    assert pd.Timestamp(r["발주기한"]) == pd.Timestamp("2026-01-03")
    assert r["발주량_kg"] == 400


def test_under_safety_days_ignore_recovery_by_inbound():
    # 재고 500, 하루 100 -> 1/5부터 안전재고(200) 아래. 1/8에 입고돼 회복하면 미달 일수는 그 전까지만 센다
    r = _row(stock=500, per_day=100, safety=2, lead=10, cycle=3, inbound=[("2026-01-08", 3000)])
    assert 0 < r["안전재고미달일수"] < 10


def test_inbound_prevents_alert():
    r = _row(stock=1000, per_day=100, safety=2, lead=2, inbound=[("2026-01-05", 6000)])
    assert r["상태"] not in ("긴급", "발주필요")
    assert r["발주량_kg"] == 0 or r["상태"] == "과잉"


def test_inbound_before_base_date_is_ignored():
    d = C.load_data(_tables(inbound=[("2025-12-30", 5000)]))
    p = C.project(d)
    assert any("이미 재고에 반영" in n for n in p.notes)
    assert p.summary.iloc[0]["입고예정_kg"] == 0


def test_overstock_is_flagged():
    r = _row(stock=100000, per_day=100, safety=2)
    assert r["상태"] == "과잉"


def test_plan_beyond_end_continues_with_average():
    d = C.load_data(_tables(per_day=100, days=5))
    p = C.project(d)
    g = p.daily[p.daily["원료"].str.contains("앞다리")]
    assert g["소요량_kg"].iloc[0] == 100 and g["소요량_kg"].iloc[20] == pytest.approx(100)
    assert any("하루 평균으로 이어" in n for n in p.notes)


def test_defaults_used_when_columns_blank():
    t = _tables()
    for c in ("안전재고일수", "리드타임일수", "발주주기일"):
        t["inventory"][c] = None
    r = C.project(C.load_data(t)).summary.iloc[0]
    assert (r["안전재고일수"], r["리드타임일수"], r["발주주기일"]) == (5, 2, 3)  # 국산 냉장 기본값


def test_settings_override_defaults():
    t = _tables()
    for c in ("안전재고일수", "리드타임일수", "발주주기일"):
        t["inventory"][c] = None
    s = C.Settings()
    s.safety[("국산", "냉장")], s.lead["국산"] = 9, 7
    r = C.project(C.load_data(t), settings=s).summary.iloc[0]
    assert (r["안전재고일수"], r["리드타임일수"]) == (9, 7)


def test_sample_tables_are_valid_and_cover_all_levels():
    d = C.load_data(C.sample_tables(pd.Timestamp("2026-10-14")))
    assert d.ready(), d.issues
    p = C.project(d)
    assert {"긴급", "발주필요", "과잉"} <= set(p.summary["상태"])  # 샘플은 모든 상태를 보여준다


# ---------------------------------------------------------------- 지시 문구
def _instr(**kw):
    d = C.load_data(_tables(**kw))
    p = C.project(d)
    return ADV.instruction(p.summary.iloc[0], ADV.Market(), p.base_date)


def test_instruction_order_has_date_and_tons():
    a = _instr(stock=500, per_day=100, safety=2, lead=2, cycle=3)
    assert a["level"] == "발주"
    assert "0.4톤" in a["headline"]       # 발주량
    assert "1/3" in a["headline"]         # 발주 기한 날짜


def test_instruction_normal_mentions_next_order_deadline():
    a = _instr(stock=1000, per_day=100, safety=2, lead=2, cycle=3)
    assert a["level"] == "정상" and "다음 발주 기한 1/8" in a["headline"]


def test_instruction_emergency_lists_spot_buy_and_affected_products():
    a = _instr(stock=150, per_day=100, safety=1, lead=5, inbound=[("2026-01-20", 3000)])
    text = " ".join(a["steps"])
    assert a["level"] == "긴급"
    assert "긴급 현물 구매" in text and "입고 앞당기기" in text and "생산계획 조정" in text and "햄" in text


def test_instruction_emergency_without_inbound_skips_pull_forward():
    text = " ".join(_instr(stock=150, per_day=100, safety=1, lead=5)["steps"])
    assert "입고 앞당기기" not in text


def test_normal_has_no_market_lines():
    a = _instr(stock=100000, per_day=10, safety=1)
    assert a["level"] in ("정상", "과잉")


# ---------------------------------------------------------------- 현장 용어
def test_plain_formats():
    assert ton(8200) == "8.2톤" and ton(50) == "50kg"
    assert md("2026-10-14") == "10/14(수)"
    assert vs_normal(-29.4) == "평소보다 29% 적음" and vs_normal(0.2) == "평소와 비슷함"
    assert vs_year(12.3) == "평년보다 12% 비쌈"
    assert deviation_word(-3.0) == "매우 적음" and deviation_word(1.6) == "꽤 많음" and deviation_word(0.1) == "비슷함"


# ---------------------------------------------------------------- 알림
@pytest.fixture
def company_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path / "company")
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path / "live")
    return tmp_path / "company"


def test_alerts_include_company_when_files_saved(company_dir):
    for kind, df in C.sample_tables().items():
        C.save_raw(kind, df.to_csv(index=False).encode("utf-8-sig"))
    found = [a for a in alerts.find_alerts(alerts.CONFIG) if a["rule"] == "company"]
    assert found and all(a["message"].startswith("우리 공장 ·") for a in found)
    assert any("발주" in a["message"] for a in found)


def test_alerts_skip_company_without_files(company_dir):
    assert not [a for a in alerts.find_alerts(alerts.CONFIG) if a["rule"] == "company"]


def test_broken_company_file_does_not_block_market_alerts(company_dir):
    C.save_raw("inventory", "엉터리,열\n1,2\n".encode("utf-8"))
    found = alerts.find_alerts(alerts.CONFIG)  # 예외 없이 끝나야 한다
    assert isinstance(found, list)


# ---------------------------------------------------------------- 비밀번호 잠금
def test_gate_open_when_no_password(monkeypatch):
    from lib import ui_company as UC
    monkeypatch.delenv("COMPANY_PASSWORD", raising=False)
    assert UC.password_set() is False and UC.unlocked() is True


def test_gate_locked_until_session_unlocked(monkeypatch):
    from lib import ui_company as UC
    monkeypatch.setenv("COMPANY_PASSWORD", "비밀1234")
    monkeypatch.setattr(UC.st, "session_state", {})
    assert UC.password_set() is True and UC.unlocked() is False
    UC.st.session_state["co_unlocked"] = True
    assert UC.unlocked() is True
