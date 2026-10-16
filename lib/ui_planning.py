"""생산관리 검토 화면: 원료 제약 → 계획 영향 → 대응안 비교 → 조치 이력."""
import json

import pandas as pd
import streamlit as st

from lib import company as C, planning as P, worklog as W


def show_impact(data, compact=False):
    try:
        result = P.simulate(data)
    except ValueError as exc:
        st.warning(str(exc))
        return None
    m = result.metrics()
    cols = st.columns(3)
    cols[0].metric("원료로 만들 수 있는 양", f"{m['원료기준가능_kg']:,.0f} kg")
    cols[1].metric("계획 대비 부족량", f"{m['생산부족_kg']:,.0f} kg")
    cols[2].metric("영향받는 생산계획", f"{m['영향계획건수']}건")
    table = result.orders
    if compact:
        table = table[table["부족량_kg"] > 1e-6].head(8)
    st.dataframe(table, hide_index=True, use_container_width=True)
    st.caption("명시된 계획만 계산합니다. 당일 우선순위(작은 숫자 우선) → 납기 → 원본 행 순서로 원료를 배분합니다. 부족분은 이후 날짜로 자동 이월하지 않습니다.")
    st.caption("납기 위험은 원료 부족 또는 계획일이 납기보다 늦은 경우입니다. 실제 납품 지연일을 예측한 값은 아닙니다. 사용기한이 빠른 로트부터 사용합니다.")
    return result


def _get_data():
    from lib import ui_company as UC
    if UC.render_lock("생산관리"):
        return None, ""
    left, right = st.columns(2)
    if left.button("샘플(가짜) 데이터로 시험", key="planning_sample"):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if right.button("올려 둔 파일 사용", key="planning_saved"):
        st.session_state["co_source"] = "saved"
        st.rerun()
    data, label = UC.current_company()
    if data is None:
        st.info("샘플을 선택하거나 '원료 소요·재고'에서 CSV를 올려 주세요.")
        return None, label
    if data.issues:
        with st.expander("입력 검증·가정", expanded=not data.ready()):
            st.dataframe(pd.DataFrame([vars(i) for i in data.issues]), hide_index=True)
    if not data.ready():
        st.error("계산 불완전: 오류를 먼저 수정해 주세요.")
        return None, label
    warning = C.freshness(data, C.today_kst())
    if warning:
        st.warning(warning)
        return None, label
    st.caption(f"{label} · 재고 마감 {data.inventory['기준일'].max():%Y-%m-%d} · 판단일 {C.today_kst():%Y-%m-%d}")
    return data, label


def render_planning():
    st.subheader("생산 차질·대응안 비교")
    st.write("원료 부족이 어떤 제품·주문에 영향을 주는지 확인하고, 대응을 바꿔 비교합니다.")
    st.info("원료 기준의 검토용 계산입니다. 설비·인력·포장재·운송·승인은 제외합니다. 자동 최적화, 실제 발주, 생산계획 변경을 실행하지 않습니다.")
    data, label = _get_data()
    if data is None:
        return
    baseline = show_impact(data)
    if baseline is None:
        return
    st.markdown("#### 대응안 입력 · 원본은 그대로 유지됩니다")
    st.caption("입고 앞당기기: 기존 행의 입고예정일 수정 / 긴급 확보: 입고 행 추가 / 생산일 변경: 계획 날짜 수정. 해동이 필요하면 사용가능일도 함께 검토하세요.")
    name = st.text_input("대응안 이름", "입고 일정 조정 검토")
    sig = P.fingerprint(data)
    with st.expander("생산계획 편집 · 날짜·수량·납기·우선순위", expanded=True):
        plan = st.data_editor(data.plan, num_rows="fixed", use_container_width=True, hide_index=True,
                              disabled=["제품", "주문번호"], key=f"plan_edit_{sig}")
    with st.expander("입고 예정 편집 · 추가 확보 물량도 여기에 입력", expanded=True):
        inbound = data.inbound
        if inbound is None:
            inbound = pd.DataFrame(columns=C.REQUIRED["inbound"] + ["보관", "공급처", "사용상태", "사용가능일", "사용기한"])
        inbound = st.data_editor(inbound, num_rows="dynamic", hide_index=True, use_container_width=True, key=f"inbound_edit_{sig}",
                                column_config={"보관": st.column_config.SelectboxColumn(options=["냉장", "냉동"]),
                                               "사용상태": st.column_config.SelectboxColumn(options=["사용가능", "검사대기", "사용보류"], default="사용가능")})
    candidate = P.change_scenario(data, plan=plan, inbound=inbound)
    if not candidate.ready():
        st.error("대응안 입력을 확인하세요. 잘못된 입력으로 개선 효과를 계산하지 않습니다.")
        st.dataframe(pd.DataFrame([vars(i) for i in candidate.issues if i.level == "오류"]), hide_index=True)
        return
    try:
        before, after, table = P.comparison(data, candidate)
    except ValueError as exc:
        st.warning(str(exc))
        return
    st.markdown("#### 기존 계획과 대응안 비교")
    st.dataframe(table, hide_index=True, use_container_width=True)
    st.caption("같은 종료일까지 비교합니다. 생산량을 줄여 부족이 사라진 경우는 개선으로 단정할 수 없습니다. 아래 주문별 계획량·납기를 함께 확인하세요. 대응안별 비용 비교는 하지 않습니다(제품 원료비와 단가 변동 영향은 '원가 영향' 화면).")
    if before.metrics()["계획량_kg"] != after.metrics()["계획량_kg"]:
        st.warning("대응안에서 총 계획량이 변경되었습니다. 감산·증산과 원료 확보 효과를 구분하세요.")
    changes = before.orders.set_index("계획행")[["주문번호", "제품", "날짜", "납기", "계획물량_kg", "생산가능_kg", "부족량_kg", "납기상태"]].join(
        after.orders.set_index("계획행")[["날짜", "계획물량_kg", "생산가능_kg", "부족량_kg", "납기상태"]], rsuffix="(대응안)")
    st.dataframe(changes, use_container_width=True)
    with st.expander("원료별 사용 가능 잔량·미충족 소요"):
        st.dataframe(after.materials, hide_index=True)
        st.caption("미충족 소요는 생산하지 못한 제품에 대응하는 투입량입니다. 해당 원료 자체가 모두 부족하다는 뜻은 아닙니다.")
    context = {"출처": label, "판단일": str(C.today_kst().date()), "기존입력ID": before.fingerprint,
               "대응입력ID": after.fingerprint, "대응안": name, "비교": table.to_dict(orient="records"),
               "기존입력": json.loads(P.snapshot(data)), "대응입력": json.loads(P.snapshot(candidate))}
    st.download_button("대응안·계산 근거 내려받기", json.dumps(context, ensure_ascii=False, indent=2), "production-scenario.json", "application/json")
    if st.button("이 대응안을 조치 기록에 연결"):
        st.session_state["pending_action"] = context
        st.success("조치 이력 화면으로 이동해 담당자·기한·협의 내용을 입력해 저장하세요. 원본 CSV와 실제 발주는 바뀌지 않았습니다.")


def render_worklog():
    from lib import ui_company as UC
    st.subheader("조치 이력")
    if UC.render_lock("조치 이력"):
        return
    sample = st.session_state.get("co_source") == "sample"
    st.caption("샘플 기록은 이 브라우저 세션에만 남습니다." if sample else "기록은 이 PC의 data/company/worklog.sqlite3에 저장됩니다. 이전 버전을 덮어쓰지 않습니다.")
    st.caption("개인정보·비밀번호·민감한 회사 내부정보는 입력하지 마세요. 상태 변경은 수동 기록이며 실제 입고나 해결을 자동 인증하지 않습니다.")
    events = st.session_state.get("sample_worklog", []) if sample else W.read_events()
    latest = {}
    for event in events:
        latest.setdefault(event["사건ID"], event)
    selected = st.selectbox("새 기록 또는 이어 기록", ["새 기록", *latest], format_func=lambda key: key if key == "새 기록" else f"{latest[key]['제목']} · {latest[key]['상태']} · {key[:6]}")
    current = latest.get(selected, {})
    pending = st.session_state.get("pending_action", {})
    correct_source = pending.get("출처") == ("샘플(가짜) 데이터" if sample else "올려 둔 파일")
    attach = st.checkbox("최근 선택한 대응안의 계산 근거를 연결", value=correct_source, disabled=not correct_source)
    context = pending if attach and correct_source else current.get("근거", {})
    if context:
        st.caption(f"연결 근거: {context.get('대응안', '')} · 입력 ID {context.get('대응입력ID', '')} · 판단일 {context.get('판단일', '')}")
    with st.form(f"worklog_{selected}"):
        title = st.text_input("기록 제목", current.get("제목", context.get("대응안", "")))
        owner = st.text_input("담당자 또는 담당 역할", current.get("담당자", ""))
        deadline = st.date_input("조치 기한", value=pd.Timestamp(current.get("기한", C.today_kst())).date())
        state = st.selectbox("진행 상태", W.STATES, index=W.STATES.index(current.get("상태", W.STATES[0])))
        action = st.text_area("선택한 대응·협의 내용·확정 입고일", current.get("대응", ""))
        result = st.text_area("확인 결과·재계산 결과·해결 근거", current.get("결과", ""))
        submitted = st.form_submit_button("새 버전으로 기록 저장")
    if submitted:
        if not title.strip() or not owner.strip() or not action.strip() or (state == "해결 확인" and not result.strip()):
            st.error("제목·담당자·대응 내용을 입력하고, 해결 확인에는 근거를 남겨 주세요.")
        else:
            case_id = None if selected == "새 기록" else selected
            if sample:
                from uuid import uuid4
                event = {"번호": len(events) + 1, "사건ID": case_id or uuid4().hex, "기록시각": str(pd.Timestamp.now(tz="Asia/Seoul")),
                         "제목": title, "담당자": owner, "기한": str(deadline), "상태": state, "대응": action, "결과": result, "근거": context}
                st.session_state["sample_worklog"] = [event, *events]
            else:
                W.append_event(title, owner, deadline, state, action, result, context, case_id)
            st.session_state.pop("pending_action", None)
            st.success("새 버전을 저장했습니다. 아래 목록에 반영하려면 다른 기록을 선택하거나 화면을 다시 열어 주세요.")
            st.rerun()
    if events:
        status_filter = st.selectbox("기록 상태 필터", ["전체", *W.STATES])
        shown = [e for e in latest.values() if status_filter == "전체" or e["상태"] == status_filter]
        st.dataframe(pd.DataFrame([{k: v for k, v in e.items() if k != "근거"} for e in shown]), hide_index=True)
        with st.expander("변경 이력 · 이전 문장과 계산 근거 보존"):
            st.json(events if selected == "새 기록" else [e for e in events if e["사건ID"] == selected])
        st.download_button("전체 조치 이력 내보내기", json.dumps(events, ensure_ascii=False, indent=2), "production-worklog.json", "application/json")
