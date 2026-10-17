"""해동 계획 화면: 다음 해동 투입 → 날짜별 해동실 투입량(용량) → 확인할 문제 → 전체 일정·내려받기."""
from __future__ import annotations

import copy

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import company as C
from lib import theme
from lib import thaw as T
from lib.plain import md, ton

DAYS = 14
WEEK = "월화수목금토일"


def _settings_box(saved: C.Settings) -> None:
    days_txt = "".join(WEEK[i] for i in sorted(saved.thaw_weekdays))
    with st.expander(f"해동 기본값·작업일·해동실 용량 (국산 {saved.thaw_days['국산']}일·{saved.thaw_loss['국산']:.1f}%, "
                     f"수입 {saved.thaw_days['수입']}일·{saved.thaw_loss['수입']:.1f}%, 투입 요일 {days_txt}, 보관 한도 {saved.thaw_hold}일, 용량 "
                     f"{f'{saved.thaw_capacity:,}kg/일' if saved.thaw_capacity else '제한 없음'})"):
        draft = copy.deepcopy(saved)
        a, b, c = st.columns(3)
        a.markdown("**해동일수 (해동 시작 → 투입)**")
        for o in C.DEFAULT_THAW_DAYS:
            draft.thaw_days[o] = int(a.number_input(f"{o} 냉동", 0, 14, int(saved.thaw_days[o]), key=f"thaw_days_{o}"))
        b.markdown("**해동 감량률 (%)**")
        for o in C.DEFAULT_THAW_LOSS:
            draft.thaw_loss[o] = float(b.number_input(f"{o} 냉동 ", 0.0, 49.0, float(saved.thaw_loss[o]), 0.5, key=f"thaw_loss_{o}"))
        c.markdown("**해동실 하루 최대 투입량**")
        draft.thaw_capacity = int(c.number_input("kg (냉동 무게, 0 = 제한 없음)", 0, 1_000_000, int(saved.thaw_capacity), 100, key="thaw_cap"))
        e, f = st.columns([2, 1])
        picked = e.multiselect("해동실에 넣을 수 있는 요일", list(range(7)), default=sorted(saved.thaw_weekdays),
                               format_func=lambda i: WEEK[i] + "요일", key="thaw_weekdays")
        draft.thaw_weekdays = sorted(int(i) for i in picked)
        draft.thaw_hold = int(f.number_input("해동 후 보관 한도 (일)", 0, 14, int(saved.thaw_hold), key="thaw_hold",
                                             help="해동이 끝난 뒤 쓰기 전까지 냉장에서 기다릴 수 있는 최대 일수. 휴무일·용량 때문에 앞당길 때 이 한도를 넘지 않습니다."))
        hol = st.text_input("휴무일 (쉼표로 구분, 예: 2026-12-25, 2027-01-01)", ", ".join(saved.holidays), key="thaw_holidays")
        draft.holidays = sorted({h.strip() for h in hol.replace(chr(10), ",").split(",") if h.strip()})
        if st.button("해동 기본값 저장 · 모든 계산과 자동 알림에 적용", key="thaw_save"):
            try:
                C.save_settings(draft)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("저장했습니다.")
                st.rerun()
        st.caption("재고·입고 예정 CSV에 해동일수·해동감량률을 적은 로트는 그 값이 우선합니다. 저장 전 편집 값은 계산에 적용하지 않습니다.")


def _load_chart(tp: T.ThawPlan, start: pd.Timestamp, layout) -> go.Figure:
    load = tp.load()
    days = pd.date_range(start, periods=DAYS)
    names = sorted(load["원료"].unique())
    colors = {n: theme.SERIES[i] for i, n in enumerate(names[: len(theme.SERIES)])}
    fig = go.Figure()
    for n in names:
        g = load[load["원료"] == n].groupby("해동시작일")["냉동투입_kg"].sum().reindex(days, fill_value=0.0)
        fig.add_trace(go.Bar(x=days, y=g.values / 1000, name=n, marker_color=colors.get(n, theme.MUTED),
                             hovertemplate="%{x|%m/%d} " + n + ": %{y:.2f}톤<extra></extra>"))
    if tp.capacity:
        fig.add_hline(y=tp.capacity / 1000, line_dash="dash", line_color=theme.STATUS["critical"], line_width=1,
                      annotation_text=f"해동실 용량 {tp.capacity / 1000:.1f}톤", annotation_position="top left",
                      annotation_font=dict(size=12, color=theme.INK2))
    fig.update_layout(barmode="stack")
    fig.update_xaxes(tickformat="%m/%d", dtick=86400000)
    fig.update_yaxes(title="톤 (냉동 무게)")
    return layout(fig, f"날짜별 해동실 투입량 (앞으로 {DAYS}일)", 340)


def _event_table(ev: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"해동 시작": ev["해동시작일"].map(md), "원료": ev["원료"], "로트": ev["로트"],
                         "넣을 냉동 무게": ev["냉동투입_kg"].map(lambda v: f"{v:,.0f}kg"), "해동 후": ev["해동후_kg"].map(lambda v: f"{v:,.0f}kg"),
                         "쓰는 날": ev["사용일"].map(md), "해동일수": ev["해동일수"].map(lambda v: f"{int(v)}일"),
                         "감량": ev["감량률"].map(lambda v: f"{v:.1f}%"),
                         "비고": [s if w == 0 else f"{s} · 해동 후 {int(w)}일 대기" for s, w in zip(ev["상태"], ev["대기일수"])]})


def today_summary(data: C.CompanyData) -> None:
    """'오늘의 생산 위험'용 한 줄: 다음 해동 투입일과 양, 확인할 문제 수."""
    if not (data.recipe["보관"] == "냉동").any():
        return
    from lib import ui_company as UC
    tp = T.plan(data, UC.get_settings())
    day, ev = T.next_start(tp, C.today_kst())
    probs = tp.problems[tp.problems["날짜"] <= tp.base_date + pd.Timedelta(days=DAYS)]
    parts = []
    if day is not None:
        top = ev.groupby("원료")["냉동투입_kg"].sum().sort_values(ascending=False)
        parts.append(f"다음 투입 **{md(day)}** {ton(float(top.sum()))} (" + ", ".join(f"{n.split(' · ')[0]} {ton(v)}" for n, v in top.items()) + ")")
    else:
        parts.append("앞으로 잡힌 해동 투입 없음")
    if len(probs):
        parts.append(f"{theme.dot('critical')}확인 필요 **{len(probs)}건** ({', '.join(sorted(set(r.split(':')[0] for r in probs['이유'])))})")
    with st.container(border=True):
        st.markdown("**해동** · " + " · ".join(parts), unsafe_allow_html=True)
        st.caption("자세한 로트별 투입 목록은 '해동 계획' 화면에서 봅니다.")


def render_thaw(k: float, layout) -> None:
    from lib import ui_company as UC
    st.subheader("해동 계획")
    st.caption("생산계획 × 배합표로 냉동 원료가 필요한 날을 구하고, **언제 어느 로트를 얼마나 해동실에 넣어야 하는지** 계산합니다. "
               "배합표 투입량은 해동 후 무게로 보고, 넣을 냉동 무게 = 필요량 ÷ (1 − 감량률)입니다.")
    if UC.render_lock("해동 계획"):
        return
    left, right, _ = st.columns([2, 2, 4])
    if left.button("샘플(가짜) 데이터로 시험", key="thaw_sample", use_container_width=True):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if right.button("올려 둔 파일 사용", key="thaw_saved", use_container_width=True):
        st.session_state["co_source"] = "saved"
        st.rerun()
    saved = UC.get_settings()
    _settings_box(saved)
    data, label = UC.current_company()
    if data is None:
        st.info("샘플을 선택하거나 '원료 소요·재고'에서 CSV를 올려 주세요.")
        return
    if not data.ready():
        st.error("계산 불완전: '원료 소요·재고' 화면의 입력 오류를 먼저 수정해 주세요.")
        return
    stale = C.freshness(data, C.today_kst())
    if stale:
        st.warning(stale + " 해동 일정은 최신 재고로만 계산합니다.")
        return
    if not (data.recipe["보관"] == "냉동").any():
        st.info("배합표에 냉동 원료가 없어 해동 계획이 필요 없습니다.")
        return
    tp = T.plan(data, saved)
    st.caption(f"{label} · 재고 기준일 {md(tp.base_date)} · 판단일 {md(C.today_kst())}")
    no_date = data.inventory[(data.inventory["보관"] == "냉동") & data.inventory["사용가능일"].isna()]
    if len(no_date):
        st.caption(f"냉동 재고 {len(no_date)}개 로트는 사용가능일이 없어 **내일 해동을 시작한다고** 보고 계산했습니다. "
                   "이미 해동실에 들어간 로트라면 재고 CSV의 사용가능일에 해동 완료일을 적으세요.")

    day, nxt = T.next_start(tp, C.today_kst())
    horizon = tp.base_date + pd.Timedelta(days=DAYS)
    ev14 = tp.events[tp.events["해동시작일"] <= horizon]
    probs = tp.problems
    c1, c2, c3 = st.columns(3)
    c1.markdown(theme.kpi_html("다음 해동 투입", f"{md(day)} {ton(float(nxt['냉동투입_kg'].sum()))}" if day is not None else "없음",
                               f"{nxt['원료'].nunique()}개 원료 · {len(nxt)}개 로트" if day is not None else "잡힌 해동 일정 없음"), unsafe_allow_html=True)
    c2.markdown(theme.kpi_html(f"앞으로 {DAYS}일 해동 투입", ton(float(ev14["냉동투입_kg"].sum())),
                               f"하루 최대 {ton(float(tp.load().groupby('해동시작일')['냉동투입_kg'].sum().max() if len(tp.events) else 0))}"), unsafe_allow_html=True)
    c3.markdown(theme.kpi_html("확인 필요", f"{len(probs)}건", "해동 시간·재고·용량 문제" if len(probs) else "문제 없음",
                               tone="critical" if len(probs) else "good"), unsafe_allow_html=True)

    if day is not None:
        st.markdown(f"#### {md(day)} 해동실 투입 목록")
        st.dataframe(_event_table(nxt), hide_index=True, use_container_width=True)
    st.plotly_chart(_load_chart(tp, tp.base_date + pd.Timedelta(days=1), layout), use_container_width=True)
    st.caption("막대는 해동을 시작하는 날 해동실에 넣는 냉동 무게입니다. 가장 늦게 넣어도 되는 날(쓰는 날 − 해동일수)로 잡고, 용량을 넘으면 하루씩 앞당깁니다."
               + ("" if tp.capacity else " 해동실 용량은 위 '해동 기본값'에서 정할 수 있습니다."))

    st.markdown("#### 확인할 문제")
    if len(probs):
        st.dataframe(pd.DataFrame({"날짜": probs["날짜"].map(md), "원료": probs["원료"],
                                   "못 맞추는 양": probs["부족_kg"].map(lambda v: f"{v:,.0f}kg"), "이유": probs["이유"]}),
                     hide_index=True, use_container_width=True)
        st.caption("날짜는 쓰는 날(용량 초과는 해동 시작일)입니다. 해동 시간 부족은 생산일을 늦추거나 냉장 원료로 대체하는 검토가 필요합니다('생산 차질·대응안').")
        timing = probs["이유"].str.startswith(("해동 시간 부족", "해동 투입일 없음", "해동 후 보관 한도")).any()
        stock = probs["이유"].str.startswith("냉동 재고 부족").any()
        seeds = (["생산일 늦추기"] if timing else []) + (["긴급 현물 구매"] if stock else []) + (["대체 원료"] if timing or stock else [])
        if seeds:
            for col, seed in zip(st.columns(len(seeds)), seeds):
                if col.button(f"'{seed}' 대응안으로 검토", key=f"thaw_to_{seed}", use_container_width=True,
                              help="생산 차질·대응안 화면으로 이동해 이 후보를 편집기에 불러옵니다. 원본은 바뀌지 않습니다."):
                    st.session_state["planning_seed"] = seed
                    from lib import nav
                    if "planning" in nav.PAGES:
                        st.switch_page(nav.PAGES["planning"])
                    st.info("'생산 차질·대응안' 화면을 열면 이 후보가 편집기에 불러와져 있습니다.")
    else:
        st.success("명시된 생산계획 기간에 해동으로 맞추지 못하는 날이 없습니다.")

    st.markdown("#### 전체 해동 일정")
    if len(tp.events):
        full = _event_table(tp.events)
        st.dataframe(full, hide_index=True, use_container_width=True)
        st.download_button("해동 지시 목록 내려받기 (CSV)", full.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"해동계획_{tp.base_date:%Y%m%d}.csv", mime="text/csv", key="thaw_dl")
    else:
        st.info("새로 잡을 해동 일정이 없습니다(이미 해동 중인 로트로 충분하거나 냉동 원료 계획이 없음).")

    with st.expander("계산 가정"):
        st.write("· 명시된 생산계획만 씁니다(계획 뒤 평균 연장 없음). 사용기한이 빠른 로트부터 씁니다. 검사대기·사용보류 로트는 쓰지 않습니다.")
        st.write("· 사용가능일을 적은 냉동 로트는 이미 해동 중인 것으로 보고 그날부터 씁니다. 그 로트의 수량에도 감량률을 적용합니다.")
        st.write("· 사용가능일이 없는 냉동 로트는 들어온 날(재고는 기준일 다음 날)부터 해동을 시작할 수 있습니다.")
        st.write("· 같은 규칙이 '원료 소요·재고'(소진일·발주량)와 '생산 차질·대응안'(생산 가능량)에도 적용됩니다. 냉동 발주량은 감량을 더한 냉동 무게입니다.")
        st.write("· 해동 시작일이 비작업 요일·휴무일이면 그 전 작업일로 당기고, 그만큼 해동 후 냉장 대기가 생깁니다. 대기가 보관 한도를 넘는 투입은 잡지 않고 '확인할 문제'로 알립니다.")
        st.write("· 보관 한도는 설명용 기준입니다. 실제 해동육 보관 기준은 사내 품질 기준을 따르세요. 해동 방법별 차이, 해동 안전성 판정은 반영하지 않습니다.")
