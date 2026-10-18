"""소비기한 화면: 폐기 예상·임박 로트 → 손실 금액 → 대응안(증산·다른 제품에 돌리기) 비교 → 생산 차질 화면으로 보내기."""
from __future__ import annotations

import copy
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import company as C
from lib import expiry as E
from lib import planning as P
from lib import theme
from lib.plain import md, ton, won_big

STATE_TONE = {"기한 지남": "critical", "폐기 예상": "critical", "임박": "warning", "기한 내 사용": "good", "계산 범위 밖": "good"}


def _settings_box(saved: C.Settings) -> None:
    with st.expander(f"소비기한 기준 (임박 냉장 {saved.expiry_warn['냉장']}일·냉동 {saved.expiry_warn['냉동']}일, "
                     f"기한 미입력 가정 냉장 {saved.shelf['냉장']}일·냉동 {saved.shelf['냉동']}일, 폐기 처리비 {saved.disposal_cost:,.0f}원/kg)"):
        draft = copy.deepcopy(saved)
        a, b, c = st.columns(3)
        a.markdown("**임박 기준 (기한까지 일수)**")
        b.markdown("**기한 미입력 로트 가정 기한**")
        for sto in ("냉장", "냉동"):
            draft.expiry_warn[sto] = int(a.number_input(sto, 0, 365, int(saved.expiry_warn[sto]), key=f"exp_warn_{sto}"))
            draft.shelf[sto] = int(b.number_input(f"{sto} ", 0, 1095, int(saved.shelf[sto]), key=f"exp_shelf_{sto}"))
        c.markdown("**폐기 처리비**")
        draft.disposal_cost = float(c.number_input("원/kg", 0.0, 100000.0, float(saved.disposal_cost), 50.0, key="exp_disposal"))
        if st.button("소비기한 기준 저장", key="exp_save"):
            try:
                C.save_settings(draft)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("저장했습니다.")
                st.rerun()
        st.caption("가정 기한은 사용기한을 안 적은 로트의 경고에만 씁니다(선입선출·생산 가능량 계산에는 넣지 않음). 해동 후 보관 한도는 '해동 계획'에서 정합니다.")


def _chart(ex: E.Expiry, layout) -> go.Figure:
    df = ex.lots[ex.lots["상태"].isin(["기한 지남", "폐기 예상", "임박"])].copy()
    df["이름"] = df["원료"].str.split(" · ").str[0] + " " + df["로트"]
    df = df.sort_values("유효기한", ascending=False)
    fig = go.Figure()
    fig.add_trace(go.Bar(y=df["이름"], x=df["기한까지사용_kg"] / 1000, name="기한까지 사용", orientation="h", marker_color=theme.BLUE,
                         hovertemplate="%{y}: 기한까지 사용 %{x:.2f}톤<extra></extra>"))
    fig.add_trace(go.Bar(y=df["이름"], x=df["폐기예상_kg"] / 1000, name="폐기 예상", orientation="h", marker_color=theme.STATUS["critical"],
                         hovertemplate="%{y}: 폐기 예상 %{x:.2f}톤<extra></extra>"))
    fig.update_layout(barmode="stack", hovermode="closest")
    fig.update_xaxes(title="톤 (매입 무게)", showgrid=True, gridcolor=theme.GRID)
    fig.update_yaxes(showgrid=False)
    return layout(fig, "확인할 로트: 기한까지 쓰는 양과 남는 양", max(220, 70 + 40 * len(df)))


def today_summary(data: C.CompanyData) -> None:
    from lib import ui_company as UC
    ex = E.analyze(data, UC.get_settings(), C.today_kst())
    w, s = ex.waste, ex.soon
    if w.empty and s.empty:
        return
    parts = []
    if len(w):
        loss = w["손실_원"].sum()
        parts.append(f"{theme.dot('critical')}폐기 예상 **{len(w)}로트 {ton(float(w['폐기예상_kg'].sum()))}**" +
                     (f" (손실 {won_big(float(loss))})" if w["손실_원"].notna().all() else ""))
    if len(s):
        parts.append(f"임박 {len(s)}로트")
    with st.container(border=True):
        st.markdown("**소비기한** · " + " · ".join(parts), unsafe_allow_html=True)
        st.caption("대응안(증산·다른 제품에 돌리기)은 '소비기한' 화면에서 비교합니다.")


def render_expiry(k: float, layout) -> None:
    from lib import ui_company as UC
    st.subheader("소비기한")
    st.caption("생산계획대로 써도 **사용기한까지 다 못 쓰고 남는 양**(폐기 예상)과 기한이 임박한 로트를 찾고, 증산·다른 제품에 돌리기로 줄일 수 있는지 비교합니다. "
               "품질·안전 판정이나 법적 기한 결정이 아닙니다.")
    if UC.render_lock("소비기한"):
        return
    left, right, _ = st.columns([2, 2, 4])
    if left.button("샘플(가짜) 데이터로 시험", key="exp_sample", width="stretch"):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if right.button("올려 둔 파일 사용", key="exp_saved", width="stretch"):
        st.session_state["co_source"] = "saved"
        st.rerun()
    settings = UC.get_settings()
    _settings_box(settings)
    data, label = UC.current_company()
    if data is None:
        st.info("샘플을 선택하거나 '원료 소요·재고'에서 CSV를 올려 주세요.")
        return
    if not data.ready():
        st.error("계산 불완전: '원료 소요·재고' 화면의 입력 오류를 먼저 수정해 주세요.")
        return
    stale = C.freshness(data, C.today_kst())
    if stale:
        st.warning(stale + " 소비기한 경고는 최신 재고로만 계산합니다.")
        return
    ex = E.analyze(data, settings, C.today_kst())
    st.caption(f"{label} · 재고 기준일 {md(ex.base_date)} · 판단일 {md(ex.reference)} · 계산 범위 {md(ex.horizon_end)}까지(60일)")
    w, s = ex.waste, ex.soon
    assumed = ex.lots["기한근거"].str.startswith("가정")
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(theme.kpi_html("폐기 예상", f"{len(w)}로트", ton(float(w["폐기예상_kg"].sum())) if len(w) else "없음",
                               tone="critical" if len(w) else "good"), unsafe_allow_html=True)
    loss = float(w["손실_원"].sum()) if len(w) and w["손실_원"].notna().all() else math.nan
    c2.markdown(theme.kpi_html("예상 손실", won_big(loss) if loss == loss else ("-" if len(w) else "0원"),
                               f"처리비 {settings.disposal_cost:,.0f}원/kg 포함" if loss == loss else "단가가 없어 금액 계산 불가" if len(w) else ""), unsafe_allow_html=True)
    c3.markdown(theme.kpi_html("임박", f"{len(s)}로트", "기한 안에 다 쓰지만 가까움" if len(s) else "없음", tone="warning" if len(s) else None), unsafe_allow_html=True)
    c4.markdown(theme.kpi_html("기한 가정 로트", f"{int(assumed.sum())}개", "사용기한 미입력" if assumed.any() else "모두 입력됨"), unsafe_allow_html=True)
    from_base = ex.lots["기한근거"].str.contains("기준일부터")
    if from_base.any():
        st.caption(f"사용기한도 입고일도 없는 재고 {int(from_base.sum())}개 로트는 가정 기한을 재고 기준일부터 셉니다. 오래된 재고라면 기한이 실제보다 길게 잡히니, "
                   "재고 CSV에 입고일이나 사용기한을 적어 주세요.")

    st.markdown("#### 1. 로트별 기한")
    shown = pd.DataFrame({
        "상태": ex.lots["상태"], "원료": ex.lots["원료"], "로트": ex.lots["로트"], "구분": ex.lots["구분"],
        "수량": ex.lots["수량_kg"].map(lambda v: f"{v:,.0f}kg"), "유효 기한": ex.lots["유효기한"].map(md),
        "기한 근거": ex.lots["기한근거"], "남은 일수": ex.lots["남은일수"].map(lambda v: f"{v}일"),
        "기한까지 사용": ex.lots["기한까지사용_kg"].map(lambda v: f"{v:,.0f}kg"),
        "폐기 예상": ex.lots["폐기예상_kg"].map(lambda v: f"{v:,.0f}kg" if v > 0.5 else "-"),
        "손실": ex.lots["손실_원"].map(lambda v: won_big(v) if v == v and v > 0.5 else "-"),
    })
    st.dataframe(shown, hide_index=True, width="stretch")
    st.caption("유효 기한 = 사용기한, 미입력은 가정 기한, 이미 해동 중인 냉동 로트는 해동 후 보관 한도 중 이른 날. "
               "60일 소요량(명시된 계획 + 계획 뒤 하루 평균)을 기한이 빠른 로트부터 쓴다고 보고, 기한일에 남는 양을 폐기 예상으로 봅니다. 수량은 매입(냉동) 무게입니다.")
    if (ex.lots["상태"] == "폐기 예상").any() or (ex.lots["상태"] == "임박").any():
        st.plotly_chart(_chart(ex, layout), width="stretch")

    st.markdown("#### 2. 폐기 예상 줄이기")
    if w.empty:
        st.success("계산 범위 안에서 폐기 예상 로트가 없습니다.")
        return
    fixes = []
    for _, r in w.iterrows():
        for make in (E.increase_fix, E.capped_increase_fix, E.divert_fix):
            f = make(data, r, settings)
            if f is not None and f.data.ready():
                fixes.append(f)
    if not fixes:
        st.info("자동으로 만들 수 있는 대응안이 없습니다. 원료를 쓰는 제품의 계획이 기한 전에 없거나, 이 원료를 대체 원료로 허용한 대체표 규칙이 없습니다.")
        return
    rows = [E.evaluate(data, f, settings, C.today_kst()) for f in fixes]
    tab = pd.DataFrame({
        "대응안": [r["대응안"] for r in rows], "대상 로트": [f.lot for f in fixes],
        "로트 폐기 예상": [f"{r['로트 폐기 예상 전']:,.0f} → {r['로트 폐기 예상 후']:,.0f}kg" for r in rows],
        "전체 폐기 손실 변화": [won_big(r["전체 폐기 손실 변화"]) if r["전체 폐기 손실 변화"] == r["전체 폐기 손실 변화"] else "-" for r in rows],
        "생산 부족 변화": [f"{r['생산 부족 변화']:+,.0f}kg" if abs(r["생산 부족 변화"]) >= 0.5 else "변화 없음" for r in rows],
        "계획량 변화": [f"{r['계획량 변화']:+,.0f}kg" if abs(r["계획량 변화"]) >= 0.5 else "-" for r in rows],
    })
    st.dataframe(tab, hide_index=True, width="stretch")
    st.caption("생산 부족이 늘어나는 안은 다른 원료를 끌어 써서 다른 계획을 못 만들게 된다는 뜻입니다. 폐기 손실이 줄어도 그대로 실행하기 어렵습니다.")
    for f, r in zip(fixes, rows):
        with st.expander(f.name + (" · 생산 부족 늘어남" if r["생산 부족 변화"] > 0.5 else "")):
            for n in f.notes:
                st.write("· " + n)
            for x in f.warnings:
                st.warning(x)
    pick = st.selectbox("생산 차질·대응안에서 이어서 다듬을 안", [f.name for f in fixes], key="exp_pick")
    if st.button("생산 차질·대응안 편집기로 보내기", key="exp_send"):
        chosen = next(f for f in fixes if f.name == pick)
        st.session_state["planning_external"] = (P.fingerprint(data), {chosen.name: chosen.data})
        st.session_state["planning_seed"] = chosen.name
        from lib import nav
        if "planning" in nav.PAGES:
            st.switch_page(nav.PAGES["planning"])
        st.info("'생산 차질·대응안' 화면을 열면 이 안이 편집기에 불러와져 있습니다.")
