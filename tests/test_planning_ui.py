"""브라우저 연결 없이 Streamlit의 공식 테스트 실행기로 화면·입력 흐름 검사."""
import pytest
from pathlib import Path
from streamlit.testing.v1 import AppTest

from lib import company as C, advice as A, ui_company as UC


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'COMPANY_DIR', tmp_path / 'company')
    monkeypatch.delenv('COMPANY_PASSWORD', raising=False)
    monkeypatch.setattr(UC, 'market_cached', lambda *args: A.Market())


def test_today_works_without_market_data(isolated):
    app = AppTest.from_string('from lib.ui_company import render_today\nrender_today(1.5)').run(timeout=30)
    app.button(key='today_sample').click().run(timeout=30)
    assert not app.exception
    assert any(m.label == '계획 대비 부족량' for m in app.metric)
    assert any('시장 참고 데이터가 없습니다' in x.value for x in app.info)


def test_planning_sample_compare_and_connect(isolated):
    app = AppTest.from_string('from lib.ui_planning import render_planning\nrender_planning()').run(timeout=30)
    app.button(key='planning_sample').click().run(timeout=30)
    assert not app.exception
    assert not app.error
    assert len(app.dataframe) >= 4
    next(b for b in app.button if b.label == '이 대응안을 조치 기록에 연결').click().run(timeout=30)
    assert not app.exception
    assert app.session_state['pending_action']['출처'] == '샘플(가짜) 데이터'
    assert not C.COMPANY_DIR.exists()


def test_worklog_sample_save_and_update(isolated):
    app = AppTest.from_string("import streamlit as st\nst.session_state['co_source']='sample'\nfrom lib.ui_planning import render_worklog\nrender_worklog()")
    app.run(timeout=30)
    app.text_input[0].set_value('입고 지연')
    app.text_input[1].set_value('구매 담당')
    app.text_area[0].set_value('입고일 협의')
    app.button[0].click().run(timeout=30)
    assert not app.exception
    events = app.session_state['sample_worklog']
    assert len(events) == 1
    app.selectbox[0].select(events[0]['사건ID']).run(timeout=30)
    app.selectbox[1].select('해결 확인')
    app.text_area[1].set_value('입고 확인 및 재계산')
    app.button[0].click().run(timeout=30)
    assert not app.exception
    assert len(app.session_state['sample_worklog']) == 2
    assert not C.COMPANY_DIR.exists()


def test_planning_and_history_respect_lock(isolated, monkeypatch):
    monkeypatch.setenv('COMPANY_PASSWORD', 'test-only-password')
    for function in ('render_planning', 'render_worklog'):
        app = AppTest.from_string(f'from lib.ui_planning import {function}\n{function}()').run(timeout=30)
        assert not app.exception
        assert app.text_input[0].label == '비밀번호'
        assert not app.dataframe


def test_full_app_entry_with_no_market(isolated):
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py')).run(timeout=30)
    assert not app.exception
    assert app.subheader[0].value == '오늘의 생산 위험'


def test_company_settings_save_used_by_screen(isolated):
    app = AppTest.from_string('from lib.ui_company import render_company\nrender_company(1.5, lambda fig,*args:fig)').run(timeout=30)
    app.number_input(key='set_lead_국산').set_value(8)
    next(b for b in app.button if b.label == '기본값 저장 · 화면과 자동 알림에 함께 적용').click().run(timeout=30)
    assert not app.exception
    assert C.load_settings().lead['국산'] == 8


def test_real_history_survives_new_screen(isolated):
    app = AppTest.from_string('from lib.ui_planning import render_worklog\nrender_worklog()').run(timeout=30)
    app.text_input[0].set_value('검토 기록')
    app.text_input[1].set_value('생산관리')
    app.text_area[0].set_value('공급처 일정 확인')
    app.button[0].click().run(timeout=30)
    assert not app.exception
    second = AppTest.from_string('from lib.ui_planning import render_worklog\nrender_worklog()').run(timeout=30)
    assert not second.exception
    assert len(second.dataframe[0].value) == 1
