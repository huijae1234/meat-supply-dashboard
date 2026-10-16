import pandas as pd
import pytest

from lib import company as C, planning as P, worklog as W, alerts, live
from test_company import _tables, BASE


def data(**kwargs):
    return C.load_data(_tables(**kwargs))


def test_shared_material_never_allocated_twice():
    raw = _tables(stock=100, per_day=100, days=1)
    raw['plan'].loc[1] = ["2026-01-02", "소시지", 100]
    raw['recipe'].loc[1] = ["소시지", "돼지", "앞다리", "국산", 1]
    raw['plan']['우선순위'] = [2, 1]
    result = P.simulate(C.load_data(raw))
    assert result.orders.set_index('제품').loc['소시지', '생산가능_kg'] == 100
    assert result.orders['생산가능_kg'].sum() == 100
    assert result.metrics()['생산부족_kg'] == 100


def test_two_ingredients_constrain_product_and_leave_other_stock():
    raw = _tables(stock=100, per_day=100, days=1)
    extra = raw['inventory'].iloc[0].copy()
    extra['부위'], extra['재고량_kg'] = '삼겹살', 20
    raw['inventory'].loc[1] = extra
    raw['recipe'].loc[1] = ['햄', '돼지', '삼겹살', '국산', 1]
    result = P.simulate(C.load_data(raw))
    assert result.orders.iloc[0]['생산가능_kg'] == 20
    assert result.materials.set_index('원료').loc['돼지 앞다리(국산) · 냉장', '사용가능잔량_kg'] == 80


def test_storage_ambiguity_blocks_legacy_recipe():
    raw = _tables()
    second = raw['inventory'].iloc[0].copy()
    second['보관'] = '냉동'
    raw['inventory'].loc[1] = second
    assert not C.load_data(raw).ready()
    raw['recipe']['보관'] = '냉장'
    d = C.load_data(raw)
    assert d.ready()
    assert len(C.project(d).summary) == 2


@pytest.mark.parametrize('status', ['검사대기', '사용보류'])
def test_blocked_lot_not_used_in_both_calculations(status):
    raw = _tables(stock=1000, days=1)
    raw['inventory']['사용상태'] = status
    d = C.load_data(raw)
    assert C.project(d).summary.iloc[0]['초기사용가능_kg'] == 0
    assert P.simulate(d).orders.iloc[0]['생산가능_kg'] == 0


def test_expiry_inclusive_and_readiness():
    raw = _tables(stock=1000, days=3)
    raw['inventory']['사용가능일'] = '2026-01-03'
    raw['inventory']['사용기한'] = '2026-01-03'
    d = C.load_data(raw)
    assert P.simulate(d).orders['생산가능_kg'].tolist() == [0, 100, 0]


def test_earlier_expiry_consumed_first():
    raw = _tables(stock=100, days=2)
    second = raw['inventory'].iloc[0].copy()
    raw['inventory'].loc[1] = second
    raw['inventory']['사용기한'] = ['2026-01-10', '2026-01-02']
    assert P.simulate(C.load_data(raw)).orders['생산가능_kg'].tolist() == [100, 100]


def test_partial_production_does_not_become_automatic_backlog():
    d = data(stock=50, per_day=100, days=2, inbound=[('2026-01-03', 200)])
    assert P.simulate(d).orders['생산가능_kg'].tolist() == [50, 100]


def test_scenario_add_inbound_and_preserve_original():
    d = data(stock=0, days=2)
    before_json = P.snapshot(d)
    incoming = pd.DataFrame([['2026-01-02', '돼지', '앞다리', '국산', 200, '냉장']], columns=C.REQUIRED['inbound'] + ['보관'])
    candidate = P.change_scenario(d, inbound=incoming)
    a, b, _ = P.comparison(d, candidate)
    assert a.metrics()['생산부족_kg'] == 200
    assert b.metrics()['생산부족_kg'] == 0
    assert P.snapshot(d) == before_json
    assert a.fingerprint != b.fingerprint


def test_delay_can_cure_material_shortage_but_not_due_date():
    raw = _tables(stock=0, days=1, inbound=[('2026-01-03', 100)])
    raw['plan']['납기'] = '2026-01-02'
    d = C.load_data(raw)
    plan = d.plan.copy()
    plan['날짜'] = pd.Timestamp('2026-01-03')
    a, b, _ = P.comparison(d, P.change_scenario(d, plan=plan))
    assert a.metrics()['생산부족_kg'] == 100
    assert b.metrics()['생산부족_kg'] == 0
    assert b.metrics()['납기위험건수'] == 1
    assert a.materials['날짜'].max() == b.materials['날짜'].max()


def test_reduced_plan_is_visible():
    d = data(stock=0, days=1)
    p = d.plan.copy()
    p['계획물량_kg'] = 0
    _, _, table = P.comparison(d, P.change_scenario(d, plan=p))
    assert table.set_index('지표').loc['계획량_kg', '변화'] == -100


def test_past_or_invalid_scenario_blocks():
    d = data()
    p = d.plan.copy()
    p['날짜'] = pd.Timestamp(BASE)
    assert not P.change_scenario(d, plan=p).ready()
    p['계획물량_kg'] = -1
    assert not P.change_scenario(d, plan=p).ready()


def test_stale_data_cannot_issue_orders():
    d = data(stock=0)
    p = C.project(d, today=pd.Timestamp('2026-01-10'))
    assert not p.actionable
    assert set(p.summary['상태']) == {'확인필요'}
    assert C.freshness(d, pd.Timestamp('2026-01-02')) == ''
    assert C.freshness(d, pd.Timestamp('2025-12-31'))


def test_far_arrival_not_extrapolated_as_order_quantity():
    raw = _tables(stock=0, lead=120)
    r = C.project(C.load_data(raw)).summary.iloc[0]
    assert r['상태'] == '확인필요' and r['발주량_kg'] == 0


@pytest.mark.parametrize('column,value', [('리드타임일수', -1), ('안전재고일수', 'bad'), ('발주주기일', 0), ('리드타임일수', 1.5), ('재고량_kg', float('inf'))])
def test_invalid_numeric_inputs_block(column, value):
    raw = _tables()
    raw['inventory'][column] = value
    assert not C.load_data(raw).ready()


def test_mixed_inventory_dates_block():
    raw = _tables()
    row = raw['inventory'].iloc[0].copy()
    row['기준일'] = '2026-01-02'
    raw['inventory'].loc[1] = row
    assert not C.load_data(raw).ready()


def test_settings_persist_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'COMPANY_DIR', tmp_path)
    s = C.Settings()
    s.lead['국산'] = 7
    C.save_settings(s)
    assert C.load_settings().lead['국산'] == 7
    s.cycle['국산'] = -1
    with pytest.raises(ValueError):
        C.save_settings(s)
    assert C.load_settings().cycle['국산'] == 3


def test_worklog_versions_survive_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'COMPANY_DIR', tmp_path)
    case = W.append_event('입고 지연', '구매', '2026-01-03', '확인 필요', '업체 확인', '', {'입력ID': 'a'})
    W.append_event('입고 지연', '구매', '2026-01-03', '해결 확인', '입고 앞당김', '재계산 부족 0', {'입력ID': 'b'}, case)
    events = W.read_events()
    assert len(events) == 2
    assert events[0]['사건ID'] == events[1]['사건ID']
    assert events[1]['근거']['입력ID'] == 'a'
    assert events[0]['상태'] == '해결 확인'
    with pytest.raises(ValueError):
        W.append_event('a', 'b', '2026-01-03', '해결 확인', 'c', '', {})


def test_alerts_use_saved_settings_and_current_date(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'COMPANY_DIR', tmp_path / 'company')
    monkeypatch.setattr(live, 'LIVE_DIR', tmp_path / 'live')
    monkeypatch.setattr(C, 'today_kst', lambda: pd.Timestamp(BASE))
    raw = _tables(stock=1000, days=14)
    for col in ['안전재고일수', '리드타임일수', '발주주기일']:
        raw['inventory'][col] = None
    for kind, frame in raw.items():
        if frame is not None:
            C.save_raw(kind, frame.to_csv(index=False).encode('utf-8-sig'))
    s = C.Settings()
    s.lead['국산'] = 12
    C.save_settings(s)
    events = [e for e in alerts.find_alerts(alerts.CONFIG) if e['rule'] == 'company']
    assert any('소진' in e['message'] for e in events)
    monkeypatch.setattr(C, 'today_kst', lambda: pd.Timestamp('2026-01-10'))
    events = [e for e in alerts.find_alerts(alerts.CONFIG) if e['rule'] == 'company']
    assert len(events) == 1 and '갱신 필요' in events[0]['message']
