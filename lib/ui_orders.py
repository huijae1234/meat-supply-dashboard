"""현장 지시서 화면: 발주서 편집 → 해동 투입 체크 → 엑셀·인쇄 → 완료를 조치 이력에 기록."""
from __future__ import annotations

import math

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from lib import company as C
from lib import orders as O
from lib import planning as P
from lib import response as R
from lib import thaw as T
from lib import theme
from lib.plain import md, ton, won_big

EDITABLE = ["포함", "공급처", "발주량_kg", "납품요청일", "비고"]


def _spot_rows(data, settings) -> pd.DataFrame | None:
    """'생산 차질·대응안'의 긴급 현물 후보가 추가한 입고 행. 세션에 계산해 둔 것이 있으면 그것을 쓴다."""
    cached = st.session_state.get("auto_candidates")
    cand = None
    if cached and cached[0][0] == P.fingerprint(data):
        cand = next((c for c in cached[1] if c.name == "긴급 현물 구매"), None)
    if cand is None:
        cand = R.spot_candidate(data, P.simulate(data, settings=settings), settings, C.today_kst())
    if cand is None or cand.data.inbound is None or "공급처" not in cand.data.inbound:
        return None
    rows = cand.data.inbound[cand.data.inbound["공급처"] == "긴급 현물(가정)"]
    return rows if len(rows) else None


def _order_editor(data, proj, settings) -> pd.DataFrame:
    st.markdown("#### 1. 발주서")
    order = O.draft(data, proj, settings)
    use_spot = st.checkbox("생산 차질·대응안의 '긴급 현물 구매' 후보 물량 넣기", key="orders_spot",
                           help="긴급 현물 후보가 추가한 입고 물량을 발주서에 넣습니다. 같은 원료의 정규 발주는 포함을 끕니다(중복 방지).")
    if use_spot:
        rows = _spot_rows(data, settings)
        if rows is None:
            st.caption("긴급 현물 후보가 없습니다(부족이 없거나 도착 전에는 줄일 수 없는 부족뿐).")
        else:
            spot = O.from_inbound(data, rows)
            dup = order["원료"].isin(spot["원료"])
            order.loc[dup, "포함"] = False
            order.loc[dup, "비고"] = (order.loc[dup, "비고"].fillna("") + " 긴급 현물로 대체(포함 해제)").str.strip()
            order = pd.concat([spot, order], ignore_index=True)
    if order.empty:
        st.success("발주 검토량이 있는 원료가 없습니다.")
        return order
    edited = st.data_editor(order, hide_index=True, use_container_width=True, key=f"orders_edit_{P.fingerprint(data)}_{use_spot}",
                            disabled=[c for c in order.columns if c not in EDITABLE],
                            column_order=["포함", "구분", "상태", "원료", "공급처", "발주량_kg", "계산량_kg", "납품요청일", "발주기한", "단가_원kg", "금액_원", "비고"],
                            column_config={"발주량_kg": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="%,.0f"),
                                           "계산량_kg": st.column_config.NumberColumn(format="%,.0f"),
                                           "단가_원kg": st.column_config.NumberColumn(format="%,.0f"),
                                           "금액_원": st.column_config.NumberColumn("금액_원(편집 전)", format="%,.0f"),
                                           "납품요청일": st.column_config.DateColumn(format="YYYY-MM-DD"),
                                           "발주기한": st.column_config.DateColumn(format="YYYY-MM-DD")})
    sel = O.selected(edited)
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(theme.kpi_html("포함한 발주", f"{len(sel)}건", f"긴급 {int((sel['상태'] == '긴급').sum())}건"), unsafe_allow_html=True)
    c2.markdown(theme.kpi_html("총 수량", ton(float(sel["발주량_kg"].sum()))), unsafe_allow_html=True)
    total = float(sel["금액_원"].sum()) if len(sel) and sel["금액_원"].notna().all() else math.nan
    c3.markdown(theme.kpi_html("금액(참고)", won_big(total) if total == total else "-", "단가 기준 참고값"), unsafe_allow_html=True)
    c4.markdown(theme.kpi_html("공급처", f"{sel['공급처'].nunique()}곳", ", ".join(sorted(sel["공급처"].unique()))[:40]), unsafe_allow_html=True)
    if (sel["공급처"].astype(str).str.contains("미지정")).any():
        st.warning("공급처가 '미지정'인 발주가 있습니다. 공급처.csv를 올리거나 표에서 공급처를 적어 주세요.")
    st.caption("발주량은 '원료 소요·재고'의 발주 검토량을 공급처의 최소 발주량·발주 단위에 맞춰 올린 값입니다. "
               "납품 요청일 = 긴급은 가장 빠른 입고일, 그 외는 발주 기한 + 리드타임. 표에서 포함·공급처·수량·납품일·비고를 고칠 수 있습니다.")
    return edited


def _thaw_editor(data, settings) -> pd.DataFrame:
    st.markdown("#### 2. 해동 투입")
    if not (data.recipe["보관"] == "냉동").any():
        st.caption("냉동 원료가 없어 해동 투입이 없습니다.")
        return pd.DataFrame()
    tp = T.plan(data, settings)
    if tp.events.empty:
        st.caption("새로 잡을 해동 투입이 없습니다.")
        return tp.events
    days = sorted(tp.events["해동시작일"].unique())
    first, _ = T.next_start(tp, C.today_kst())
    default = days.index(first) if first is not None and first in days else 0
    span = st.select_slider("지시서에 넣을 해동 시작일", options=days, value=(days[default], days[min(default + 2, len(days) - 1)]),
                            format_func=md, key="orders_thaw_span")
    ev = tp.events[(tp.events["해동시작일"] >= span[0]) & (tp.events["해동시작일"] <= span[1])].copy()
    ev.insert(0, "투입 완료", False)
    edited = st.data_editor(ev, hide_index=True, use_container_width=True, key=f"orders_thaw_{P.fingerprint(data)}_{span[0]}_{span[1]}",
                            disabled=[c for c in ev.columns if c != "투입 완료"],
                            column_order=["투입 완료", "해동시작일", "원료", "로트", "냉동투입_kg", "해동후_kg", "사용일", "대기일수", "상태"],
                            column_config={"해동시작일": st.column_config.DateColumn(format="MM/DD"), "사용일": st.column_config.DateColumn(format="MM/DD"),
                                           "냉동투입_kg": st.column_config.NumberColumn("넣을 냉동 무게(kg)", format="%,.0f"),
                                           "해동후_kg": st.column_config.NumberColumn("해동 후(kg)", format="%,.0f")})
    if len(tp.problems):
        st.caption(f"해동으로 맞추지 못하는 문제 {len(tp.problems)}건은 '해동 계획' 화면에서 확인하세요.")
    return edited


def _recorded_thaw(events: list[dict], label: str) -> set[tuple[str, str]]:
    out = set()
    for e in events:
        ctx = e.get("근거") or {}
        if ctx.get("종류") == "해동 투입" and ctx.get("출처") == label:
            out |= {(str(r.get("해동시작일", ""))[:10], str(r.get("로트", ""))) for r in ctx.get("해동", [])}
    return out


def today_summary(data, label: str) -> None:
    """'오늘의 생산 위험'용 한 줄: 아직 발주 완료로 기록하지 않은 발주와, 다음 해동 투입 중 기록하지 않은 건."""
    from lib import ui_company as UC
    from lib import worklog as W
    settings = UC.get_settings()
    proj = C.project(data, today=C.today_kst(), settings=settings)
    if not proj.actionable:
        return
    order = O.draft(data, proj, settings)
    todo = order[order["포함"]]
    parts = [f"발주 미기록 **{len(todo)}건**" + (f" (긴급 {int((todo['상태'] == '긴급').sum())})" if len(todo) else "")]
    if (data.recipe["보관"] == "냉동").any():
        tp = T.plan(data, settings)
        day, ev = T.next_start(tp, C.today_kst())
        if day is not None:
            events = st.session_state.get("sample_worklog", []) if label.startswith("샘플") else W.read_events()
            done = _recorded_thaw(events, label)
            left = [r for _, r in ev.iterrows() if (f"{r['해동시작일']:%Y-%m-%d}", str(r["로트"])) not in done]
            parts.append(f"{md(day)} 해동 투입 미기록 **{len(left)}건**")
    with st.container(border=True):
        st.markdown("**지시서** · " + " · ".join(parts))
        st.caption("'현장 지시서'에서 발주서·해동 지시서를 만들고 완료를 기록합니다.")


def render_orders(k: float, layout) -> None:
    from lib import ui_company as UC
    from lib.ui_planning import add_record
    st.subheader("현장 지시서")
    st.caption("계산 결과로 **발주서와 해동 지시서**를 만들고, 엑셀로 내려받거나 인쇄합니다. 실제 발주를 보내지 않으며, 완료 여부는 조치 이력에 사람이 기록합니다.")
    if UC.render_lock("현장 지시서"):
        return
    left, right, _ = st.columns([2, 2, 4])
    if left.button("샘플(가짜) 데이터로 시험", key="orders_sample", use_container_width=True):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if right.button("올려 둔 파일 사용", key="orders_saved", use_container_width=True):
        st.session_state["co_source"] = "saved"
        st.rerun()
    data, label = UC.current_company()
    if data is None:
        st.info("샘플을 선택하거나 '원료 소요·재고'에서 CSV를 올려 주세요.")
        return
    if not data.ready():
        st.error("계산 불완전: '원료 소요·재고' 화면의 입력 오류를 먼저 수정해 주세요.")
        return
    stale = C.freshness(data, C.today_kst())
    if stale:
        st.warning(stale + " 지시서는 최신 재고로만 만듭니다.")
        return
    sample = label.startswith("샘플")
    if sample:
        st.warning("샘플(가짜) 데이터입니다. 만든 문서에도 '샘플·가짜 데이터'가 표시됩니다. 실제 발주에 쓰지 마세요.")
    settings = UC.get_settings()
    proj = C.project(data, today=C.today_kst(), settings=settings)
    st.caption(f"{label} · 재고 기준일 {md(proj.base_date)} · 판단일 {md(C.today_kst())}")

    order = _order_editor(data, proj, settings)
    thaw = _thaw_editor(data, settings)

    st.markdown("#### 3. 내려받기·인쇄")
    who = st.text_input("작성자 (이름 대신 역할도 됩니다)", key="orders_author", placeholder="예: 생산관리 담당")
    meta = {"발주일": f"{C.today_kst():%Y-%m-%d}", "기준일": f"{proj.base_date:%Y-%m-%d}", "작성자": who, "샘플": sample,
            "공급처정보": {} if data.supplier is None else {r["공급처"]: {"담당자": r["담당자"], "연락처": r["연락처"]} for _, r in data.supplier.iterrows()}}
    thaw_doc = thaw.drop(columns=["투입 완료"]) if len(thaw) else thaw
    order_doc = order if len(order) else pd.DataFrame(columns=O.COLUMNS)
    a, b, c = st.columns(3)
    a.download_button("발주서·해동 지시서 (엑셀)", O.workbook(order_doc, thaw_doc, meta), file_name=f"현장지시서_{C.today_kst():%Y%m%d}.xlsx",
                      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, key="orders_xlsx")
    page = O.print_html(order_doc, thaw_doc, meta)
    b.download_button("인쇄용 지시서 (HTML)", page.encode("utf-8"), file_name=f"현장지시서_{C.today_kst():%Y%m%d}.html", mime="text/html",
                      use_container_width=True, key="orders_html")
    add = O.inbound_rows(order_doc)
    c.download_button("입고예정 추가용 행 (CSV)", add.to_csv(index=False).encode("utf-8-sig"), file_name="입고예정_추가.csv", mime="text/csv",
                      use_container_width=True, key="orders_inbound", disabled=add.empty,
                      help="발주가 확정되면 이 행을 입고예정.csv에 붙여 다시 올리세요. 원본은 자동으로 바뀌지 않습니다.")
    with st.expander("인쇄 미리보기", expanded=False):
        components.html(page, height=640, scrolling=True)
        st.caption("미리보기 안의 '인쇄' 버튼은 지시서만 인쇄합니다(A4).")

    st.markdown("#### 4. 완료 기록 → 조치 이력")
    if st.session_state.get("orders_msg"):
        st.success(st.session_state.pop("orders_msg"))
    st.caption("체크·기록은 사람이 확인한 실행 결과입니다. 원본 CSV(입고 예정·재고 사용가능일)는 바뀌지 않습니다."
               + (" 샘플 기록은 이 브라우저 세션에만 남습니다." if sample else ""))
    owner = st.text_input("기록하는 사람(역할)", key="orders_owner", placeholder="예: 생산관리 담당")
    x, y = st.columns(2)
    sel = O.selected(order_doc)
    if x.button(f"포함한 발주 {len(sel)}건을 '발주 완료'로 기록", disabled=sel.empty, use_container_width=True, key="orders_done"):
        if not owner.strip():
            st.error("기록하는 사람(역할)을 적어 주세요.")
        else:
            lines = [f"{r['공급처']} · {r['원료']} {r['발주량_kg']:,.0f}kg · 납품 요청 {pd.Timestamp(r['납품요청일']):%m/%d}" for _, r in sel.iterrows()]
            ctx = {"출처": label, "판단일": str(C.today_kst().date()), "종류": "발주 완료",
                   "발주": sel.drop(columns=["포함"]).astype(str).to_dict(orient="records"),
                   "입고추가": O.inbound_rows(sel).astype(object).where(O.inbound_rows(sel).notna(), None).to_dict(orient="records")}
            add_record(f"발주 완료: {sel['공급처'].nunique()}곳 {len(sel)}건", owner, C.today_kst().date(), "실행 완료", "\n".join(lines),
                       "입고예정.csv에 반영 필요(입고예정 추가용 행 내려받기)", ctx)
            st.session_state["orders_msg"] = "조치 이력에 기록했습니다. 입고예정.csv에 반영하기 전까지 이 물량을 입고 예정(미반영)으로 계산에 넣습니다."
            st.rerun()
    st.session_state.setdefault("apply_pending", True)
    st.toggle("발주 완료 기록을 입고 예정(미반영)으로 계산에 넣기", key="apply_pending",
              help="같은 원료를 다음 날 또 발주필요로 띄우지 않기 위한 임시 반영입니다. 원본 CSV는 바뀌지 않습니다.")
    done = thaw[thaw["투입 완료"]] if len(thaw) else thaw
    if y.button(f"체크한 해동 투입 {len(done)}건 기록", disabled=len(done) == 0, use_container_width=True, key="thaw_done"):
        if not owner.strip():
            st.error("기록하는 사람(역할)을 적어 주세요.")
        else:
            lines = [f"{pd.Timestamp(r['해동시작일']):%m/%d} {r['원료']} 로트 {r['로트']} {r['냉동투입_kg']:,.0f}kg 투입 (사용 {pd.Timestamp(r['사용일']):%m/%d})"
                     for _, r in done.iterrows()]
            ctx = {"출처": label, "판단일": str(C.today_kst().date()), "종류": "해동 투입",
                   "해동": done.drop(columns=["투입 완료"]).astype(str).to_dict(orient="records")}
            add_record(f"해동 투입 완료: {len(done)}건", owner, C.today_kst().date(), "실행 완료", "\n".join(lines),
                       "재고 CSV의 사용가능일에 해동 완료일 반영 필요", ctx)
            st.success("조치 이력에 기록했습니다. 다음 업로드 때 해당 로트의 사용가능일을 적으면 계산에 반영됩니다.")
