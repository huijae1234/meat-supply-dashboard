"""생산관리 검토 화면: 원료 제약 → 계획 영향 → 대응안 비교 → 조치 이력."""
import copy
import json
import math

import pandas as pd
import streamlit as st

from lib import company as C, planning as P, response as R, worklog as W
from lib import public_demo


def show_impact(data, compact=False):
    try:
        from lib import ui_company as UC
        result = P.simulate(data, settings=UC.get_settings())
    except ValueError as exc:
        st.warning(str(exc))
        return None
    m = result.metrics()
    cols = st.columns(3)
    cols[0].metric("원료·능력으로 만들 수 있는 양" if m.get("능력부족_kg", 0) > 0 else "원료로 만들 수 있는 양", f"{m['원료기준가능_kg']:,.0f} kg")
    cols[1].metric("계획 대비 부족량", f"{m['생산부족_kg']:,.0f} kg")
    cols[2].metric("영향받는 생산계획", f"{m['영향계획건수']}건")
    table = result.orders
    if compact:  # 오늘 화면용 요약: 부족한 계획만, 날짜·kg로 읽기 쉽게
        from lib.plain import md
        t = table[table["부족량_kg"] > 1e-6].head(8)
        kg = lambda v: f"{v:,.0f}kg"  # noqa: E731
        table = pd.DataFrame({"날짜": t["날짜"].map(md), "제품": t["제품"], "주문번호": t["주문번호"], "계획": t["계획물량_kg"].map(kg),
                              "만들 수 있는 양": t["생산가능_kg"].map(kg), "부족": t["부족량_kg"].map(kg), "부족 원인": t["부족원료"],
                              "납기": t["납기"].map(md), "납기 상태": t["납기상태"]})
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption("명시된 계획만 계산합니다. 당일 우선순위(작은 숫자 우선) → 납기 → 원본 행 순서로 원료를 배분합니다. 부족분은 이후 날짜로 자동 이월하지 않습니다.")
    st.caption("납기 위험은 원료 부족 또는 계획일이 납기보다 늦은 경우입니다. 실제 납품 지연일을 예측한 값은 아닙니다. 사용기한이 빠른 로트부터 사용합니다.")
    if m.get("능력부족_kg", 0) > 0.5:
        st.caption(f"이 중 라인 하루 능력 때문에 못 만드는 양 {m['능력부족_kg']:,.0f}kg (부족원료 칸의 '생산 능력'). '생산 차질·대응안'에서 앞뒤 작업일로 나눈 안을 볼 수 있습니다.")
    return result


def _capacity_settings(data, saved) -> None:
    from lib import capacity as Q
    lines_now = ", ".join(f"{k} {v:,.0f}kg" for k, v in saved.line_capacity.items() if v) or "미설정"
    with st.expander(f"생산 라인·하루 최대 생산량 ({lines_now}, 앞뒤 {saved.shift_days}작업일까지 옮김)"):
        products = sorted(p for p in set(data.plan["제품"]) if "(대체" not in p)
        plan_line = data.plan.groupby("제품")["라인"].agg(lambda x: next((v for v in x if str(v) not in ("", "nan", "None")), "")) if "라인" in data.plan else pd.Series(dtype=str)
        t1 = pd.DataFrame({"제품": products, "라인": [saved.lines.get(p) or plan_line.get(p, "") for p in products]})
        a, b = st.columns(2)
        with a:
            st.markdown("**제품 → 라인**")
            t1 = st.data_editor(t1, hide_index=True, width="stretch", disabled=["제품"], key=f"cap_lines_{P.fingerprint(data)}")
            if "라인" in data.plan:
                st.caption("생산계획에 '라인' 열이 있으면 그 값이 이 표보다 우선합니다.")
        names = sorted({str(v).strip() for v in t1["라인"] if str(v).strip() not in ("", "nan", "None")} | set(saved.line_capacity)
                       | ({str(v) for v in data.plan["라인"] if str(v) not in ("", "nan", "None")} if "라인" in data.plan else set()))
        t2 = pd.DataFrame({"라인": names, "하루 최대(kg)": [float(saved.line_capacity.get(n, 0)) for n in names]})
        with b:
            st.markdown("**라인 하루 최대 생산량**")
            t2 = st.data_editor(t2, hide_index=True, width="stretch", disabled=["라인"], key=f"cap_caps_{'|'.join(names)}",
                                column_config={"하루 최대(kg)": st.column_config.NumberColumn(min_value=0.0, step=100.0, format="%,.0f")})
            st.caption("0이면 제한 없음. 작업일은 '해동 계획'의 투입 요일·휴무일을 함께 씁니다.")
        shift = st.number_input("넘치는 양을 앞뒤 몇 작업일까지 옮길까요", 0, 14, int(saved.shift_days), key="cap_shift")
        if st.button("생산 능력 저장 · 모든 계산에 적용", key="cap_save", disabled=public_demo.enabled()):
            draft = copy.deepcopy(saved)
            draft.lines = {r["제품"]: str(r["라인"]).strip() for _, r in t1.iterrows() if str(r["라인"]).strip() not in ("", "nan", "None")}
            draft.line_capacity = {r["라인"]: float(r["하루 최대(kg)"] or 0) for _, r in t2.iterrows() if (r["하루 최대(kg)"] or 0) > 0}
            draft.shift_days = int(shift)
            try:
                C.save_settings(draft)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("저장했습니다.")
                st.rerun()


def _capacity_section(data, settings) -> None:
    from lib import capacity as Q
    st.markdown("#### 생산 능력")
    if st.session_state.get("co_source") == "sample":
        on = st.session_state.get("sample_capacity", False)
        if st.button("샘플 예시 능력 끄기" if on else "샘플 예시 능력으로 보기", key="sample_capacity_btn",
                     help="햄 6,000 · 베이컨 1,300 · 탕류 1,000 · 양념육 500kg/일. 이 브라우저 세션의 샘플 계산에만 쓰고 설정 파일에는 저장하지 않습니다."):
            st.session_state["sample_capacity"] = not on
            st.rerun()
        if on:
            st.caption("샘플 예시 능력이 켜져 있습니다(모든 화면의 샘플 계산에 적용, 저장 안 함): "
                       + ", ".join(f"{k} {v:,}kg" for k, v in C.SAMPLE_CAPACITY.items()))
    _capacity_settings(data, settings)
    if not Q.enabled(data, settings):
        st.caption("라인별 하루 최대 생산량을 정하면 능력을 넘는 날을 찾아 앞뒤 작업일로 나눈 안을 보여 줍니다.")
        return
    over = Q.overloads(data, settings)
    if over.empty:
        st.success("라인 하루 능력을 넘는 날이 없습니다.")
        return
    st.dataframe(pd.DataFrame({"라인": over["라인"], "날짜": over["날짜"].dt.strftime("%m/%d"), "계획량": over["계획량_kg"].map(lambda v: f"{v:,.0f}kg"),
                               "하루 능력": over["능력_kg"].map(lambda v: f"{v:,.0f}kg"), "넘는 양": over["초과_kg"].map(lambda v: f"{v:,.0f}kg")}),
                 hide_index=True, width="stretch")
    lv = Q.level(data, settings)
    if len(lv.moves):
        st.markdown("**나눈 안** — 납기 안에서 앞당기기를 먼저(그날까지 원료가 들어와 있어야 함), 안 되면 뒤로 미룹니다.")
        st.dataframe(pd.DataFrame({"원래 날짜": lv.moves["원래날짜"].dt.strftime("%m/%d"), "제품": lv.moves["제품"], "주문번호": lv.moves["주문번호"],
                                   "옮긴 날짜": lv.moves["옮긴날짜"].dt.strftime("%m/%d"), "물량": lv.moves["물량_kg"].map(lambda v: f"{v:,.0f}kg"),
                                   "방향": lv.moves["방향"], "납기": lv.moves["납기상태"]}), hide_index=True, width="stretch")
    if len(lv.overflow):
        st.warning("옮길 곳이 없어 능력을 넘는 날: " + ", ".join(f"{r['날짜']:%m/%d} {r['라인']} {r['초과_kg']:,.0f}kg" for _, r in lv.overflow.iterrows())
                   + f". 앞뒤 {settings.shift_days}작업일 안에 남은 능력이 없습니다(옮길 범위를 늘리거나 감산·잔업을 검토).")
    out = lv.plan.copy()
    for c in ("날짜", "납기"):
        if c in out:
            out[c] = pd.to_datetime(out[c]).dt.strftime("%Y-%m-%d")
    st.download_button("조정된 생산계획 내려받기 (CSV)", out.to_csv(index=False).encode("utf-8-sig"), file_name="생산계획_능력조정.csv", mime="text/csv",
                       key="cap_download", help="확인 후 '원료 소요·재고'에서 생산계획.csv로 다시 올리면 반영됩니다. 원본은 자동으로 바뀌지 않습니다.")
    st.caption("이 안은 아래 '자동 대응 후보'의 '생산 능력 맞춰 나누기'로도 비교·편집할 수 있습니다.")


def _get_data():
    from lib import ui_company as UC
    if UC.render_lock("생산관리"):
        return None, ""
    left, right = st.columns(2)
    if left.button("샘플(가짜) 데이터로 시험", key="planning_sample"):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if not public_demo.enabled() and right.button("올려 둔 파일 사용", key="planning_saved"):
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


def _spot_settings(saved: C.Settings) -> None:
    with st.expander(f"긴급 현물 가정 (할증 {saved.spot_premium:.0f}%, 도착 국산 {saved.spot_lead['국산']}일·수입 {saved.spot_lead['수입']}일)"):
        draft = copy.deepcopy(saved)
        a, b, c = st.columns(3)
        draft.spot_premium = float(a.number_input("단가 할증 (%)", 0.0, 200.0, float(saved.spot_premium), 1.0, key="spot_premium"))
        draft.spot_lead = {"국산": int(b.number_input("도착 일수 · 국산", 0, 30, int(saved.spot_lead["국산"]), key="spot_lead_dom")),
                           "수입": int(c.number_input("도착 일수 · 수입", 0, 30, int(saved.spot_lead["수입"]), key="spot_lead_imp"))}
        if st.button("긴급 현물 가정 저장", key="spot_save", disabled=public_demo.enabled()):
            try:
                C.save_settings(draft)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("저장했습니다.")
                st.rerun()
        st.caption("판단일부터 도착 일수 뒤에 들어온다고 봅니다. 냉동은 해동 일정까지 더해 쓸 수 있는 날을 계산합니다.")


def _won(v) -> str:
    from lib.plain import won_big
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else won_big(v)


def _auto_candidates(data, settings) -> dict:
    """자동 대응 후보 표. {후보 이름: 후보 데이터}를 돌려준다(편집기 불러오기용)."""
    st.markdown("#### 자동 대응 후보")
    st.caption("부족을 보고 규칙으로 만든 후보입니다(최적화 아님). 대체 원료는 대체표에 허용된 것만, 긴급 현물은 아래 가정으로 만듭니다.")
    _spot_settings(settings)
    ext_state = st.session_state.get("planning_external")  # 다른 화면(소비기한 등)에서 보낸 대응안
    ext = ext_state[1] if ext_state and ext_state[0] == P.fingerprint(data) else {}
    # 편집기를 고칠 때마다 화면이 다시 그려지므로, 같은 입력·설정·판단일이면 후보 계산을 다시 하지 않는다
    key = (P.fingerprint(data), repr(sorted((k, repr(v)) for k, v in vars(settings).items())), str(C.today_kst().date()))
    cached = st.session_state.get("auto_candidates")
    if cached and cached[0] == key:
        cands, table = cached[1], cached[2]
    else:
        try:
            _, cands = R.candidates(data, settings, C.today_kst())
        except ValueError as exc:
            st.warning(str(exc))
            return dict(ext)
        table = R.compare(data, cands, settings) if len(cands) > 1 else None
        st.session_state["auto_candidates"] = (key, cands, table)
    if len(cands) == 1 and not ext:
        st.success("부족이 없어 대응 후보가 필요 없습니다.")
        return {}
    if len(cands) == 1:
        st.success("부족이 없어 자동 대응 후보는 없습니다. 다른 화면에서 보낸 안을 편집기에 불러올 수 있습니다.")
    if data.substitute is None:
        st.caption("대체표.csv가 없어 대체 원료 후보는 만들지 않았습니다('원료 소요·재고'에서 올릴 수 있습니다).")
    shown = pd.DataFrame() if table is None else pd.DataFrame({
        "대응안": table["대응안"], "생산 부족": table["생산부족_kg"].map(lambda v: f"{max(v, 0):,.0f}kg"),
        "영향 계획": table["영향계획건수"].map(lambda v: f"{v}건"), "납기 위험": table["납기위험건수"].map(lambda v: f"{v}건"),
        "남은 감산 손실": table["남은감산손실"].map(_won), "위약금": table["위약금"].map(_won), "추가 비용": table["추가비용"].map(_won),
        "합계(참고)": table["합계"].map(_won), "확인할 점": table["경고"],
    })
    if table is not None:
        st.dataframe(shown, hide_index=True, width="stretch")
    if table is None:
        pass
    elif table["합계"].isna().all():
        st.caption("판매가·단가가 없어 금액은 계산하지 않았습니다. 생산계획의 판매가_원kg, 재고·입고의 단가_원kg를 넣으면 비교합니다.")
    else:
        st.caption("합계 = 남은 감산 손실(공헌이익) + 위약금 + 추가 비용(긴급 현물 할증·대체 원료비 차이). 계획 물량을 모두 만든다는 보장이 아니며, 금액이 작다고 실행 가능한 안이라는 뜻은 아닙니다.")
    for c in cands[1:]:
        with st.expander(f"{c.name} · 무엇을 바꿨나" + (" · 확인 필요" if c.warnings else "")):
            for n in c.notes:
                st.write("· " + n)
            for w in c.warnings:
                st.warning(w)
    names = [c.name for c in cands] + [n for n in ext if n not in {c.name for c in cands}]
    if ext:
        st.caption("다른 화면에서 보낸 안: " + ", ".join(ext))
    current = st.session_state.get("planning_seed", "감산 수용")
    pick = st.selectbox("편집기에 불러올 대응안", names, index=names.index(current) if current in names else 0)
    if pick != current:
        st.session_state["planning_seed"] = pick
        st.rerun()
    return {**{c.name: c.data for c in cands}, **ext}


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
    from lib import ui_company as UC
    settings = UC.get_settings()
    _capacity_section(data, settings)
    seed_data = _auto_candidates(data, settings)
    st.markdown("#### 대응안 입력 · 원본은 그대로 유지됩니다")
    st.caption("입고 앞당기기: 기존 행의 입고예정일 수정 / 긴급 확보: 입고 행 추가 / 생산일 변경: 계획 날짜 수정 / 대체: 배합표에서 원료·투입량 변경. "
               "해동이 필요하면 사용가능일도 함께 검토하세요.")
    seed = st.session_state.get("planning_seed", "감산 수용")
    base_data = seed_data.get(seed, data)
    name = st.text_input("대응안 이름", seed if seed != "감산 수용" else "입고 일정 조정 검토", key=f"scenario_name_{seed}")
    sig = P.fingerprint(data) + "_" + P.fingerprint(base_data)
    with st.expander("생산계획 편집 · 날짜·수량·납기·우선순위", expanded=True):
        plan = st.data_editor(base_data.plan, num_rows="fixed", width="stretch", hide_index=True,
                              disabled=["제품", "주문번호"], key=f"plan_edit_{sig}")
    with st.expander("입고 예정 편집 · 추가 확보 물량도 여기에 입력", expanded=True):
        inbound = base_data.inbound
        if inbound is None:
            inbound = pd.DataFrame(columns=C.REQUIRED["inbound"] + ["보관", "공급처", "사용상태", "사용가능일", "사용기한"])
        inbound = st.data_editor(inbound, num_rows="dynamic", hide_index=True, width="stretch", key=f"inbound_edit_{sig}",
                                column_config={"보관": st.column_config.SelectboxColumn(options=["냉장", "냉동"]),
                                               "사용상태": st.column_config.SelectboxColumn(options=["사용가능", "검사대기", "사용보류"], default="사용가능")})
    with st.expander("배합표 편집 · 대체 배합", expanded=seed == "대체 원료"):
        recipe = st.data_editor(base_data.recipe, num_rows="dynamic", hide_index=True, width="stretch", key=f"recipe_edit_{sig}",
                                column_config={"보관": st.column_config.SelectboxColumn(options=["냉장", "냉동"]),
                                               "원산지": st.column_config.SelectboxColumn(options=["국산", "수입"])})
        st.caption("대체 배합은 '제품명 (대체)'처럼 새 제품으로 두고 생산계획의 해당 날짜만 그 제품으로 바꾸면 원래 배합과 섞이지 않습니다.")
    candidate = P.change_scenario(data, plan=plan, inbound=inbound, recipe=recipe)
    if not candidate.ready():
        st.error("대응안 입력을 확인하세요. 잘못된 입력으로 개선 효과를 계산하지 않습니다.")
        st.dataframe(pd.DataFrame([vars(i) for i in candidate.issues if i.level == "오류"]), hide_index=True)
        return
    try:
        before, after, table = P.comparison(data, candidate, settings)
    except ValueError as exc:
        st.warning(str(exc))
        return
    st.markdown("#### 기존 계획과 대응안 비교")
    m0, m1 = R.money(data, before, settings), R.money(candidate, after, settings)
    m0["추가비용"], m1["추가비용"] = 0.0, R.extra_cost(data, candidate, settings)
    for m in (m0, m1):
        m["합계"] = m["감산손실"] + m["위약금"] + m["추가비용"]
    money_rows = pd.DataFrame([{"지표": f"{k}_원", "기존": m0[k], "대응안": m1[k], "변화": m1[k] - m0[k]} for k in ("감산손실", "위약금", "추가비용", "합계")])
    st.dataframe(pd.concat([table, money_rows], ignore_index=True), hide_index=True, width="stretch")
    st.caption("같은 종료일까지 비교합니다. 생산량을 줄여 부족이 사라진 경우는 개선으로 단정할 수 없습니다. 아래 주문별 계획량·납기를 함께 확인하세요. "
               "감산손실은 공헌이익(판매가 − 원료비) 기준입니다. 추가비용 = 늘어난 입고의 단가 할증분(입고 행의 단가_원kg − 재구매 단가) + 배합 변경으로 바뀐 원료비. "
               "입고 행에 단가를 안 적으면 할증 0으로 봅니다.")
    if before.metrics()["계획량_kg"] != after.metrics()["계획량_kg"]:
        st.warning("대응안에서 총 계획량이 변경되었습니다. 감산·증산과 원료 확보 효과를 구분하세요.")
    changes = before.orders.set_index("계획행")[["주문번호", "제품", "날짜", "납기", "계획물량_kg", "생산가능_kg", "부족량_kg", "납기상태"]].join(
        after.orders.set_index("계획행")[["날짜", "계획물량_kg", "생산가능_kg", "부족량_kg", "납기상태"]], rsuffix="(대응안)")
    st.dataframe(changes, width="stretch")
    with st.expander("원료별 사용 가능 잔량·미충족 소요"):
        st.dataframe(after.materials, hide_index=True)
        st.caption("미충족 소요는 생산하지 못한 제품에 대응하는 투입량입니다. 해당 원료 자체가 모두 부족하다는 뜻은 아닙니다.")
    label_changes = R.origin_changes(data, candidate)
    if label_changes:
        st.warning("원산지 표시 확인이 필요한 대응안입니다: " + "; ".join(label_changes) +
                   ". 조치 기록에 연결하면 확인 항목(표시사항·포장재·고객 승인·품질·적용 로트)이 함께 들어갑니다.")
    context = {"출처": label, "판단일": str(C.today_kst().date()), "기존입력ID": before.fingerprint,
               "원산지확인": label_changes, "확인항목": R.LABEL_CHECKS if label_changes else [],
               "대응입력ID": after.fingerprint, "대응안": name, "비교": table.to_dict(orient="records"),
               "기존입력": json.loads(P.snapshot(data)), "대응입력": json.loads(P.snapshot(candidate))}
    st.download_button("대응안·계산 근거 내려받기", json.dumps(context, ensure_ascii=False, indent=2), "production-scenario.json", "application/json")
    if st.button("이 대응안을 조치 기록에 연결"):
        st.session_state["pending_action"] = context
        st.success("조치 이력 화면으로 이동해 담당자·기한·협의 내용을 입력해 저장하세요. 원본 CSV와 실제 발주는 바뀌지 않았습니다.")


def add_record(title, owner, deadline, state, action, result, context, case_id=None) -> str:
    """조치 이력에 한 건 남긴다. 샘플 모드는 브라우저 세션에만, 실제 파일 모드는 worklog.sqlite3에."""
    if st.session_state.get("co_source") == "sample":
        from uuid import uuid4
        events = st.session_state.get("sample_worklog", [])
        if not str(title).strip() or not str(owner).strip() or not str(action).strip():
            raise ValueError("제목·담당자·대응 내용을 입력하세요.")
        event = {"번호": len(events) + 1, "사건ID": case_id or uuid4().hex, "기록시각": str(pd.Timestamp.now(tz="Asia/Seoul")),
                 "제목": title, "담당자": owner, "기한": str(deadline), "상태": state, "대응": action, "결과": result, "근거": context}
        st.session_state["sample_worklog"] = [event, *events]
        return event["사건ID"]
    return W.append_event(title, owner, deadline, state, action, result, context, case_id)


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
        if context.get("확인항목"):
            st.warning("원산지 표시 확인 항목이 대응 내용에 들어가 있습니다. 확인한 항목은 □를 ■로 바꾸고, 결과 칸에 확인 근거를 남기세요.")
    with st.form(f"worklog_{selected}"):
        title = st.text_input("기록 제목", current.get("제목", context.get("대응안", "")))
        owner = st.text_input("담당자 또는 담당 역할", current.get("담당자", ""))
        deadline = st.date_input("조치 기한", value=pd.Timestamp(current.get("기한", C.today_kst())).date())
        state = st.selectbox("진행 상태", W.STATES, index=W.STATES.index(current.get("상태", W.STATES[0])))
        checks = context.get("확인항목", []) if not current else []
        default_action = current.get("대응", "") or ("\n".join(["[원산지 표시 확인] " + "; ".join(context.get("원산지확인", []))] + [f"□ {c}" for c in checks]) if checks else "")
        ca = context.get("원가가정") if not current else None
        if ca and not default_action:
            diff = ca["계획기간원료비_가정"] - ca["계획기간원료비_현재"]
            default_action = (f"[원가 가정] {ca['이름']} ({ca['단가기준']})\n"
                              + "\n".join(f"- {k} {v:+.1f}%" for k, v in ca["가정"].items())
                              + f"\n계획 기간 원료비 {diff:+,.0f}원" + (f", 원료비율 {ca['원료비율_현재']}% → {ca['원료비율_가정']}%" if ca.get("원료비율_현재") is not None else ""))
        action = st.text_area("선택한 대응·협의 내용·확정 입고일", default_action, height=160 if checks else None)
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
